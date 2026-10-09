import asyncio
import importlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


class DesktopOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "operations.sqlite"
        self.db.touch(mode=0o600)
        try:
            self.module = importlib.import_module("ai_secretary.desktop_owner")
        except ModuleNotFoundError:
            self.module = None

    def ownership(self, **kwargs):
        self.assertIsNotNone(self.module, "OS desktop ownership must exist")
        return self.module.DesktopOwnership(self.db, **kwargs)

    async def test_same_process_rival_contends_until_owner_closes_without_replacing_inode(self):
        first = await self.ownership().acquire()
        inode = first.path.stat().st_ino
        self.assertEqual(first.path.name, "desktop-dispatch.lock")
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        first.validate()
        with self.assertRaises(TimeoutError):
            await self.ownership(timeout=0.04, poll_interval=0.005).acquire()
        first.release()
        with self.assertRaises(RuntimeError):
            _ = first.fd
        with self.assertRaises(RuntimeError):
            first.release()
        async with self.ownership() as second:
            self.assertEqual(second.path.stat().st_ino, inode)

    async def test_cancelled_waiter_closes_its_descriptor_and_does_not_release_owner(self):
        async with self.ownership() as owner:
            waiter = asyncio.create_task(self.ownership(timeout=10).acquire())
            await asyncio.sleep(0.03)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            owner.validate()
            with self.assertRaises(TimeoutError):
                await self.ownership(timeout=0.02).acquire()
        async with self.ownership() as owner:
            owner.validate()

    async def test_rejects_symlink_or_public_lock_and_parent(self):
        manager = self.ownership()
        target = self.root / "other"
        target.touch(mode=0o600)
        lock = self.root / "desktop-dispatch.lock"
        lock.symlink_to(target)
        with self.assertRaises(self.module.DesktopOwnershipError):
            await manager.acquire()
        lock.unlink()
        lock.touch(mode=0o644)
        with self.assertRaises(self.module.DesktopOwnershipError):
            await manager.acquire()
        lock.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaises(self.module.DesktopOwnershipError):
            await manager.acquire()
        self.root.chmod(0o700)

    async def test_rejects_replaced_lock_inode_without_unlinking_replacement(self):
        owner = await self.ownership().acquire()
        old = self.root / "retained-lock"
        owner.path.rename(old)
        owner.path.touch(mode=0o600)
        with self.assertRaises(self.module.DesktopOwnershipError):
            owner.validate()
        replacement = owner.path.stat().st_ino
        owner.release()
        self.assertEqual(owner.path.stat().st_ino, replacement)

    async def test_inherited_child_retains_lock_after_parent_release(self):
        owner = await self.ownership().acquire()
        ready = self.root / "child-ready"
        child = subprocess.Popen([sys.executable, "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
            str(ready)], pass_fds=(owner.fd,))
        try:
            for _ in range(100):
                if ready.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(ready.exists())
            owner.release()
            with self.assertRaises(TimeoutError):
                await self.ownership(timeout=0.03).acquire()
        finally:
            child.kill()
            child.wait()
        async with self.ownership() as next_owner:
            next_owner.validate()

    async def test_parent_death_cannot_transfer_lock_while_inherited_child_lives(self):
        self.ownership()
        child_pid = self.root / "child-pid"
        script = """
import asyncio, os, pathlib, subprocess, sys
from ai_secretary.desktop_owner import DesktopOwnership
async def run():
    owner = await DesktopOwnership(sys.argv[1]).acquire()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], pass_fds=(owner.fd,))
    pathlib.Path(sys.argv[2]).write_text(str(child.pid))
    os._exit(0)
asyncio.run(run())
"""
        parent = subprocess.Popen([sys.executable, "-c", script, str(self.db), str(child_pid)])
        try:
            await asyncio.to_thread(parent.wait, 5)
            self.assertEqual(parent.returncode, 0)
            self.assertTrue(child_pid.exists())
            with self.assertRaises(TimeoutError):
                await self.ownership(timeout=0.03).acquire()
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if child_pid.exists():
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
        for _ in range(100):
            try:
                owner = await self.ownership(timeout=0.02).acquire()
                break
            except TimeoutError:
                await asyncio.sleep(0.01)
        else:
            self.fail("dead inherited child must release kernel ownership")
        owner.release()


if __name__ == "__main__":
    unittest.main()
