"""Retained OS ownership of the desktop, independent of database lease expiry."""

import asyncio
import errno
import fcntl
import math
import os
from pathlib import Path
import stat


class DesktopOwnershipError(RuntimeError):
    """The private stable desktop lock could not be established or verified."""


def _private(metadata, kind, mode=None):
    return (metadata.st_uid == os.getuid() and stat.S_IFMT(metadata.st_mode) == kind
            and (stat.S_IMODE(metadata.st_mode) == mode if mode is not None
                 else stat.S_IMODE(metadata.st_mode) & 0o077 == 0))


def _open_parent(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if not _private(os.fstat(descriptor), stat.S_IFDIR, 0o700):
        os.close(descriptor)
        raise DesktopOwnershipError("desktop authority directory must be owned and mode 700")
    return descriptor


class DesktopOwner:
    """An exclusive flock open description retained until all native work ends.

    Closing this descriptor deliberately does not call LOCK_UN: a native child
    may still inherit the same open description after its parent dies.
    """

    def __init__(self, db_path, descriptor):
        self.db_path = db_path
        self.path = db_path.parent / "desktop-dispatch.lock"
        self._fd = descriptor
        metadata = os.fstat(descriptor)
        self._identity = (metadata.st_dev, metadata.st_ino)
        self._dispatching = False

    @property
    def fd(self):
        if self._fd is None:
            raise RuntimeError("desktop ownership has ended")
        return self._fd

    def validate(self):
        descriptor = self.fd
        parent = challenger = None
        try:
            metadata = os.fstat(descriptor)
            if (not _private(metadata, stat.S_IFREG, 0o600)
                    or (metadata.st_dev, metadata.st_ino) != self._identity):
                raise DesktopOwnershipError("desktop lock descriptor changed")
            parent = _open_parent(self.db_path.parent)
            challenger = os.open(self.path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            current = os.fstat(challenger)
            if (not _private(current, stat.S_IFREG, 0o600)
                    or (current.st_dev, current.st_ino) != self._identity):
                raise DesktopOwnershipError("desktop lock path no longer identifies retained inode")
            try:
                fcntl.flock(challenger, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
            else:
                raise DesktopOwnershipError("exclusive desktop ownership is no longer retained")
        except OSError as exc:
            raise DesktopOwnershipError("desktop ownership is unavailable") from exc
        finally:
            if challenger is not None:
                os.close(challenger)
            if parent is not None:
                os.close(parent)

    def _begin_dispatch(self):
        self.validate()
        if self._dispatching:
            raise DesktopOwnershipError("desktop owner already has native work in flight")
        self._dispatching = True

    def _end_dispatch(self):
        if not self._dispatching:
            raise RuntimeError("desktop owner has no native work in flight")
        self._dispatching = False

    def release(self):
        descriptor = self.fd
        if self._dispatching:
            raise DesktopOwnershipError("native child cleanup must finish before releasing ownership")
        self._fd = None
        os.close(descriptor)


class DesktopOwnership:
    """Acquire a stable mode-600 lock beside an existing private operations DB."""

    def __init__(self, db_path, *, timeout=5, poll_interval=0.02):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval)
                or poll_interval <= 0):
            raise ValueError("desktop ownership bounds must be positive and finite")
        self.db_path = Path(os.path.abspath(Path(db_path).expanduser()))
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._context_owner = None

    async def acquire(self):
        parent = descriptor = database = None
        try:
            parent = _open_parent(self.db_path.parent)
            database = os.open(self.db_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=parent)
            if not _private(os.fstat(database), stat.S_IFREG):
                raise DesktopOwnershipError("operations database must be an owned private regular file")
            os.close(database)
            database = None
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                descriptor = os.open("desktop-dispatch.lock", flags | os.O_CREAT | os.O_EXCL,
                                     0o600, dir_fd=parent)
                os.fchmod(descriptor, 0o600)
            except FileExistsError:
                descriptor = os.open("desktop-dispatch.lock", flags, dir_fd=parent)
            if not _private(os.fstat(descriptor), stat.S_IFREG, 0o600):
                raise DesktopOwnershipError("desktop lock must be an owned mode-600 regular file")
            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("desktop ownership acquisition timed out") from exc
                    await asyncio.sleep(min(self.poll_interval, remaining))
                else:
                    owner = DesktopOwner(self.db_path, descriptor)
                    owner.validate()
                    descriptor = None
                    return owner
        except OSError as exc:
            if isinstance(exc, TimeoutError):
                raise
            raise DesktopOwnershipError("private desktop lock could not be opened") from exc
        finally:
            for opened in (database, descriptor, parent):
                if opened is not None:
                    os.close(opened)

    async def __aenter__(self):
        if self._context_owner is not None:
            raise RuntimeError("desktop ownership context is already active")
        self._context_owner = await self.acquire()
        return self._context_owner

    async def __aexit__(self, exc_type, exc, traceback):
        owner = self._context_owner
        if owner is None:
            raise RuntimeError("desktop ownership context has ended")
        owner.release()
        self._context_owner = None
