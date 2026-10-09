#!/usr/bin/env python3
"""Ownership-aware AI Secretary installation using Ghost-ALICE primitives.

Managed program files live below the chosen prefix. The addon's discovery
sidecar is published to the explicitly selected Ghost-ALICE registry as part of
the same owned transaction. Mutable worker/OpenClaw state remains unowned.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import importlib
import json
import os
from pathlib import Path
import platform
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import urllib.request
import uuid


OPENCLAW_VERSION = "2026.9.8"
OPENCLAW_INTEGRITY = "sha512-G+JkNUhtpDE3cXR4AEi2NyyG9fqI/T2WUSl8ZnR8AATH8Dh1kC3qYFL7wwPoZtgHiP/cszA86PEiE0PDysxb9Q=="
NODE_VERSION = "24.21.0"
SCHEMA_VERSION = 2
SOURCE_PARTS = ("src", "native", "scripts/build_native.sh", "scripts/installer.py", "openclaw-plugin", "addons", "addons-manifest.json", "pyproject.toml", "README.md", "README_ko.md", "LICENSE", "NOTICE", "ARCHITECTURE.md", "THIRD_PARTY_NOTICES.md")
REGISTRY_PLATFORMS = ("codex", "claude")
IGNORED = {"__pycache__", ".DS_Store", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".build", ".swiftpm"}
_FRAMEWORK_PUBLISH_LOCK = threading.RLock()


class InstallError(RuntimeError):
    """A provision or ownership check failed before a safe install was possible."""


def _exists(path):
    return os.path.lexists(path)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(path):
    path = Path(path)
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        return {"kind": "file", "sha256": _hash(path), "executable": bool(path.stat().st_mode & 0o111)}
    if path.is_dir():
        return {"kind": "directory"}
    raise InstallError(f"unsupported managed resource: {path}")


def _inventory(root, *, source_exclusions=False):
    root = Path(root)
    if not _exists(root):
        return {}
    output = {".": _entry(root)}
    if root.is_dir() and not root.is_symlink():
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not source_exclusions or name not in IGNORED)
            for name in sorted(dirs + files):
                if source_exclusions and (name in IGNORED or name.endswith((".pyc", ".pyo"))):
                    continue
                child = Path(current) / name
                output[child.relative_to(root).as_posix()] = _entry(child)
    return output


def _tree_identity(path):
    if not _exists(path):
        return "missing"
    return hashlib.sha256(json.dumps(_inventory(path), sort_keys=True).encode()).hexdigest()


def _json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _json_object(path, label):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise InstallError(f"{label} must be a JSON object; preserve it for review")
    return value


def _run(arguments, *, cwd=None, env=None):
    try:
        completed = subprocess.run([str(arg) for arg in arguments], cwd=cwd, env=env,
            text=True, capture_output=True, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"provision command failed: {arguments[0]}: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise InstallError(f"provision command exited {completed.returncode}: {arguments[0]}\n{detail}")
    return completed.stdout.strip()


def _copy(source, destination):
    source, destination = Path(source), Path(destination)
    if source.is_symlink():
        raise InstallError(f"source root must not be a symlink: {source}")
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED, "*.pyc", "*.pyo"))
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise InstallError(f"required source is missing: {source}")


class Installer:
    def __init__(self, prefix, skills_dirs, source, ghost_root, *, registry_dir=None, platform_registries=None, openclaw_source=None, node_source=None, native_backend="builtin"):
        # Do not resolve an existing prefix symlink into an unrelated directory.
        absolute_prefix = Path(os.path.abspath(Path(prefix).expanduser()))
        self.prefix = absolute_prefix.parent.resolve() / absolute_prefix.name
        absolute_skills = [Path(os.path.abspath(Path(p).expanduser())) for p in skills_dirs]
        self.skills_dirs = [path.parent.resolve() / path.name for path in absolute_skills]
        # Library callers remain isolated unless they explicitly select a
        # registry. The CLI selects normal Ghost-ALICE discovery by default.
        if registry_dir is not None and platform_registries is not None:
            raise InstallError("--registry-dir and --platform-registry cannot be mixed")
        self._typed_registries = platform_registries is not None
        if self._typed_registries:
            if not isinstance(platform_registries, dict) or not platform_registries:
                raise InstallError("platform registries must be a nonempty platform-to-absolute-directory map")
            if any(key not in REGISTRY_PLATFORMS for key in platform_registries):
                raise InstallError("unknown registry platform; supported platforms are codex and claude")
            self._registry_inputs = {key: Path(platform_registries[key]).expanduser() for key in REGISTRY_PLATFORMS if key in platform_registries}
            if any(not path.is_absolute() for path in self._registry_inputs.values()):
                raise InstallError("platform registry directories must be absolute paths")
        else:
            self._registry_inputs = {"codex": Path(os.path.abspath(Path(registry_dir).expanduser())) if registry_dir is not None else self.prefix / "registry"}
        self.platform_registries = {key: path.parent.resolve() / path.name for key, path in self._registry_inputs.items()}
        if len(set(self.platform_registries.values())) != len(self.platform_registries):
            raise InstallError("duplicate platform registry directories")
        primary = "codex" if "codex" in self.platform_registries else "claude"
        self._registry_input = self._registry_inputs[primary]
        self.registry_dir = self.platform_registries[primary]
        self.registry_path = self.registry_dir / "ai-secretary.json"
        self.source = Path(source).expanduser().resolve()
        self.ghost_root = Path(ghost_root).expanduser().resolve()
        self.openclaw_source = Path(openclaw_source).expanduser().resolve() if openclaw_source else None
        self.node_source = Path(node_source).expanduser().resolve() if node_source else None
        if native_backend not in ("builtin", "peekaboo"):
            raise InstallError("native backend must be builtin or peekaboo")
        self.native_backend = native_backend
        self.lock_path = self.prefix.parent / ("." + self.prefix.name + ".install.lock")
        self.manifest_path = self.prefix / "install-manifest.json"
        self.journal_path = self.prefix / ".install-journal.json"
        self._framework = None
        if not self.skills_dirs:
            raise InstallError("at least one skills directory is required")
        destinations = [self.prefix, *[p / "ai-secretary" for p in self.skills_dirs]]
        for left in destinations:
            for right in destinations:
                if left != right and (left in right.parents or right in left.parents):
                    raise InstallError("installation prefix and skill targets must not overlap")
        if len(set(self.skills_dirs)) != len(self.skills_dirs):
            raise InstallError("duplicate skills directories")
        for directory in self._registries().values():
            for owned_tree in [self.prefix / "managed", self.prefix / "state", *[p / "ai-secretary" for p in self.skills_dirs]]:
                if directory == owned_tree or owned_tree in directory.parents or directory / "ai-secretary.json" == owned_tree:
                    raise InstallError("registry directory must not overlap runtime, mutable state, or skill resources")

    def _registries(self):
        return self.platform_registries if self._typed_registries else {"codex": self.registry_dir}

    def _registry_map(self):
        return {key: str(path) for key, path in self._registries().items()}

    def _registry_paths(self):
        return [directory / "ai-secretary.json" for directory in self._registries().values()]

    def framework(self):
        if self._framework is None:
            shared = self.ghost_root / "_shared"
            required = ("addon_installer", "addon_registry", "install_transaction", "install_lock")
            for name in required:
                if not (shared / (name + ".py")).is_file():
                    raise InstallError(f"Ghost-ALICE installer framework is missing: {shared / (name + '.py')}")
            sys.path.insert(0, str(shared))
            modules = [importlib.import_module(name) for name in required]
            for module in modules:
                if Path(module.__file__).resolve().parent != shared:
                    raise InstallError("Ghost-ALICE module loaded from an unexpected root")
            self._framework = SimpleNamespace(addon=modules[0], registry=modules[1],
                transaction=modules[2], lock=modules[3])
        return self._framework

    def _safe_paths(self):
        for target in [self.prefix, *self.skills_dirs, *self._registry_inputs.values(), *self._registries().values()]:
            for path in [target, *target.parents]:
                if path.is_symlink():
                    raise InstallError(f"installation path traverses a symlink: {path}")

    @contextmanager
    def _one_lock(self, path):
        framework = self.framework()
        if _exists(path):
            if path.is_symlink() or not path.is_file():
                raise InstallError(f"install lock is not a regular file; preserve it for review: {path}")
            try:
                data = _json_object(path, "install lock")
                pid = data.get("pid")
                if not isinstance(pid, int) or pid <= 0:
                    raise InstallError("install lock has no verifiable owner; preserve it for review")
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    path.unlink()
                except PermissionError:
                    raise InstallError(f"install lock belongs to an active process: {pid}")
                else:
                    raise InstallError(f"install lock belongs to an active process: {pid}")
            except (ValueError, OSError) as exc:
                raise InstallError(f"cannot establish ownership of install lock: {path}") from exc
        token = "ai-secretary:" + uuid.uuid4().hex
        try:
            framework.lock.acquire_lock(path, owner=token, stale_seconds=2**63 - 1)
        except framework.lock.InstallLockError as exc:
            raise InstallError(str(exc)) from exc
        try:
            yield
        finally:
            try:
                if _json_object(path, "install lock").get("owner") == token:
                    framework.lock.release_lock(path)
            except (OSError, ValueError, InstallError):
                pass

    @contextmanager
    def _lock(self, *, runtime_guard=False):
        self._safe_paths()
        with self._one_lock(self.lock_path):
            # Same-addon installs at different prefixes share this lock. Other
            # addon sidecars and their locks are never read or rewritten.
            with ExitStack() as locks:
                for directory in sorted(self._registries().values(), key=str):
                    locks.enter_context(self._one_lock(directory / ".ai-secretary.install.lock"))
                if runtime_guard:
                    locks.enter_context(self._runtime_guard())
                    self._recover()
                elif self.journal_path.exists():
                    with self._runtime_guard():
                        self._recover()
                yield

    @staticmethod
    def _private_file(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077):
                raise InstallError(f"runtime authority file must be owned and private: {path}")
        finally:
            os.close(descriptor)

    @contextmanager
    def _runtime_guard(self):
        """Retain real process ownership through every destructive mutation.

        Stable lock files live in mutable state and are never replaced, unlinked
        or explicitly unlocked. An inherited open description remains protected
        after its original parent exits.
        """
        self._check_state_paths()
        directory = self.prefix / "state/secretary"
        with ExitStack() as retained:
            if directory.exists():
                parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                retained.callback(os.close, parent)
                metadata = os.fstat(parent)
                if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise InstallError("runtime authority directory must be owned and mode 700")
                for name, actor in (("runtime-use.lock", "runtime client"),
                                    ("desktop-dispatch.lock", "desktop ownership"),
                                    ("scheduler-projection.lock", "scheduler producer")):
                    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                    try:
                        descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                        os.fchmod(descriptor, 0o600)
                    except FileExistsError:
                        descriptor = os.open(name, flags, dir_fd=parent)
                    retained.callback(os.close, descriptor)
                    metadata = os.fstat(descriptor)
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600):
                        raise InstallError(f"private {actor} lock is unverifiable; preserve it: {name}")
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise InstallError(f"active {actor}; stop owned clients, run ai-secretary stop and ai-secretary schedule stop before retrying") from exc
                self._check_database_quiescence(directory / "operations.sqlite")
            self._check_gateway_owners()
            yield

    def _check_database_quiescence(self, path):
        if not _exists(path):
            return
        try:
            self._private_file(path)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                checks = (
                    ("operations", "state='Applying'", "Applying operation; run ai-secretary stop"),
                    ("worker_sessions", "state='Active'", "active worker; run ai-secretary stop"),
                    ("execution_contexts", "state IN('Active','Draining')", "active execution authority; run ai-secretary stop"),
                    ("execution_lifecycle", "enabled<>0 OR status<>'Stopped'", "running execution lifecycle; run ai-secretary stop and ai-secretary schedule stop"),
                    ("scheduler_outbox", "phase<>'Removed'", "unresolved scheduler obligation; run ai-secretary schedule stop"),
                    ("scheduler_producers", "state IN('Creating','Unknown')", "unresolved scheduler producer; run ai-secretary schedule stop; an uncertain create must be accounted before removal"),
                )
                for table, predicate, message in checks:
                    if table in tables and db.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1").fetchone():
                        raise InstallError(message)
                authority = {"authority_schema_version", "execution_lifecycle", "execution_contexts", "execution_operations",
                    "source_guard", "authority_sources", "collection_grants", "authority_runs", "authority_retrievals"}
                if tables & authority:
                    if not authority <= tables or db.execute("SELECT version FROM authority_schema_version").fetchall() != [(1,)]:
                        raise InstallError("partial or unsupported authority schema; preserve state for review")
                    if "worker_sessions" in tables and "execution_admissions" not in tables:
                        raise InstallError("worker authority schema is incomplete; preserve state for review")
                elif "worker_sessions" in tables:
                    raise InstallError("worker authority schema is incomplete; preserve state for review")
                scheduler = {"scheduled_jobs", "scheduled_occurrences", "scheduler_outbox", "scheduler_producers"}
                if tables & scheduler and not scheduler <= tables:
                    raise InstallError("partial scheduler authority schema; preserve state for review")
                db.rollback()
        except (sqlite3.Error, OSError) as exc:
            raise InstallError("runtime authority database cannot be verified; preserve state before retrying") from exc

    def _check_gateway_owners(self):
        """Inspect only this install's pinned state-local ownership records."""
        state = self.prefix / "state/openclaw"
        directory = state / "tmp" / ("openclaw-" + str(os.getuid()))
        if not _exists(directory):
            return
        for path in (directory, directory.parent):
            if path.is_symlink() or not path.is_dir():
                raise InstallError("Gateway owner directory is unverifiable; preserve it for review")
        records = [directory / "gateway.state.lock", directory / ("gateway." + hashlib.sha256(str(self.prefix / "state/openclaw.json").encode()).hexdigest()[:8] + ".lock"),
            directory / ("state." + hashlib.sha256(str(state / "state/openclaw.sqlite").encode()).hexdigest()[:16] + ".lock")]
        for record in records:
            if not _exists(record):
                continue
            try:
                self._private_file(record)
                payload = _json_object(record, "Gateway owner record")
                pid = payload.get("pid")
                if type(pid) is not int or pid <= 0 or payload.get("stateDir") != str(state):
                    raise InstallError("Gateway ownership is unverifiable; preserve its state and stop the original client before retrying")
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                raise InstallError("Gateway or state client is still active; stop it through its original terminal or service manager before retrying")
            except (ValueError, OSError) as exc:
                raise InstallError("Gateway ownership cannot be verified; preserve the record and stop the original client before retrying") from exc

    def _allowed_move(self, path, journal):
        raw = str(path)
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts or os.path.normpath(raw) != raw:
            return False
        targets = [self.prefix / "managed", self.manifest_path]
        containers = [self.prefix / ".ghost-alice-install-staging", Path(journal["work_root"]),
            self.prefix.parent / ("." + self.prefix.name + ".rollbacks")]
        for directory in self.skills_dirs:
            targets.append(directory / "ai-secretary")
            containers.append(directory / ".ghost-alice-install-staging")
        recorded_registries = journal.get("platform_registries", {"codex": journal.get("registry_dir")})
        if recorded_registries == self._registry_map():
            for directory in self._registries().values():
                targets.append(directory / "ai-secretary.json")
                registry_stage = directory / ".ghost-alice-install-staging"
                if path.parent == registry_stage and path.name.startswith("stage-ai-secretary.json-"):
                    containers.append(registry_stage)
        if path not in targets and path.parent not in containers:
            return False
        return not any(parent.is_symlink() for parent in path.parents)

    def _recover(self):
        if not self.journal_path.exists():
            return
        try:
            journal = _json_object(self.journal_path, "installation journal")
            if journal.get("prefix") != str(self.prefix) or journal.get("schema_version") != 1:
                raise InstallError("installation journal identity is invalid")
            if "registry_dir" in journal or "platform_registries" in journal:
                recorded_registries = journal.get("platform_registries", {"codex": journal.get("registry_dir")})
                if recorded_registries != self._registry_map():
                    raise InstallError("registry selection differs from the interrupted install; use its original registry arguments")
            work_root = Path(journal["work_root"])
            if work_root.parent != self.prefix.parent or not work_root.name.startswith("." + self.prefix.name + ".stage-"):
                raise InstallError("installation journal staging path is invalid")
            moves = journal["moves"]
            if not isinstance(moves, list) or any(not isinstance(move, dict) for move in moves):
                raise InstallError("installation journal move list is invalid")
            if not isinstance(journal.get("committed", False), bool):
                raise InstallError("installation journal commit state is invalid")
            for move in moves:
                if not self._allowed_move(move["source"], journal) or not self._allowed_move(move["dest"], journal):
                    raise InstallError("installation journal contains an unowned path")
            if work_root.exists() and _tree_identity(work_root) != journal.get("work_root_hash"):
                raise InstallError(f"interrupted installation staging changed; preserve it for review: {work_root}")
            if journal.get("committed"):
                self._cleanup_transaction_paths(moves, check_only=True)
            # Simulate the entire reversal first. Any publication/stage/backup
            # drift blocks recovery before a single resource is moved/deleted.
            # Source-copy filters must never hide later user-owned additions.
            virtual = {value: _tree_identity(value) for move in moves for value in (move["source"], move["dest"])}
            if not journal.get("committed"):
                for move in reversed(moves):
                    source, dest = move["source"], move["dest"]
                    expected = move.get("source_hash")
                    if not isinstance(expected, str):
                        raise InstallError("journal lacks resource ownership; preserve interrupted resources for review")
                    if virtual[dest] != "missing" and virtual[source] == "missing":
                        if virtual[dest] != expected:
                            raise InstallError(f"interrupted install resource changed; recovery is blocked and preserved: {dest}")
                        virtual[source], virtual[dest] = virtual[dest], "missing"
                    elif virtual[dest] != "missing" and virtual[source] != "missing":
                        raise InstallError(f"ambiguous interrupted install; preserve both resources: {dest}")
                    elif virtual[source] != "missing" and virtual[source] != expected:
                        raise InstallError(f"interrupted staging resource changed; recovery is blocked and preserved: {source}")
            for move in ([] if journal.get("committed") else reversed(list(moves))):
                source, dest = Path(move["source"]), Path(move["dest"])
                if not self._allowed_move(source, journal) or not self._allowed_move(dest, journal):
                    raise InstallError("installation journal contains an unowned path")
                if _exists(dest) and not _exists(source):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    # Record reverse actions before their atomic rename. A
                    # second interruption can then reverse this history too.
                    journal["moves"].append({"source": str(dest), "dest": str(source), "source_hash": move["source_hash"]})
                    _json_write(self.journal_path, journal)
                    os.replace(dest, source)
                elif _exists(dest) and _exists(source):
                    raise InstallError(f"ambiguous interrupted install; preserve both resources: {dest}")
            self._cleanup_transaction_paths(moves)
            self.journal_path.unlink()
            shutil.rmtree(work_root, ignore_errors=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InstallError("interrupted installation could not be safely recovered") from exc

    def _cleanup_transaction_paths(self, moves, *, check_only=False):
        rollback_root = self.prefix.parent / ("." + self.prefix.name + ".rollbacks")
        candidates = {}
        for move in moves:
            for value in (move["source"], move["dest"]):
                path = Path(value)
                if path.parent == rollback_root or path.parent.name == ".ghost-alice-install-staging":
                    candidates[path] = move.get("source_hash")
        for path, expected in candidates.items():
            if _exists(path) and (not isinstance(expected, str) or _tree_identity(path) != expected):
                raise InstallError(f"transaction cleanup resource changed; preserve it for review: {path}")
            if _exists(path) and not os.access(path.parent, os.W_OK | os.X_OK):
                raise InstallError(f"transaction cleanup parent is not writable; preserve it for retry: {path.parent}")
            if path.is_dir() and not path.is_symlink():
                for directory, _, _ in os.walk(path, followlinks=False):
                    if not os.access(directory, os.W_OK | os.X_OK):
                        raise InstallError(f"transaction cleanup directory is not writable; preserve it for retry: {directory}")
        if not check_only:
            for path in candidates:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)

    def _manifest(self):
        if not self.manifest_path.exists():
            return None
        try:
            manifest = _json_object(self.manifest_path, "installation manifest")
            version = manifest.get("schema_version")
            if type(version) is not int or version not in (1, SCHEMA_VERSION) or manifest.get("prefix") != str(self.prefix):
                raise InstallError("installation manifest identity is invalid")
            if version == SCHEMA_VERSION:
                recorded_registries = manifest.get("platform_registries", {"codex": manifest.get("registry_dir")})
                if recorded_registries != self._registry_map() or manifest.get("registry_dir") != str(self.registry_dir):
                    raise InstallError("registry selection differs from the owned install; uninstall using its original registry arguments, then reinstall with the new selection")
            expected_skills = {str(p / "ai-secretary") for p in self.skills_dirs}
            recorded_skills = {item["path"] for item in manifest["resources"] if item["role"] == "skill"}
            if expected_skills != recorded_skills:
                raise InstallError("skills directory selection differs from the owned install; use the original --skills-dir arguments")
            expected = {("runtime", str(self.prefix / "managed")), *[("skill", path) for path in expected_skills]}
            if version == SCHEMA_VERSION:
                expected.update(("registry", str(path)) for path in self._registry_paths())
            recorded = [(resource["role"], resource["path"]) for resource in manifest["resources"]]
            if len(recorded) != len(expected) or set(recorded) != expected:
                raise InstallError("manifest resource ownership does not match this install")
            for resource in manifest["resources"]:
                target = Path(resource["path"])
                if target != self.prefix / "managed" and str(target) not in expected_skills and not (version == SCHEMA_VERSION and target in self._registry_paths()):
                    raise InstallError("manifest contains a resource outside this install")
                if resource["role"] == "registry" and (set(resource["entries"]) != {"."} or resource["entries"]["."].get("kind") != "file"):
                    raise InstallError("registry ownership must describe exactly its regular sidecar file")
                if resource["role"] == "registry" and "platform_registries" in manifest:
                    resource_platform = resource.get("platform")
                    if resource_platform not in recorded_registries or str(target) != str(Path(recorded_registries[resource_platform]) / "ai-secretary.json"):
                        raise InstallError("registry resource platform does not match the owned registry selection")
                for relative in resource["entries"]:
                    if relative != "." and (Path(relative).is_absolute() or ".." in Path(relative).parts):
                        raise InstallError("manifest contains an unsafe relative resource")
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InstallError("installation manifest is unreadable; preserve existing resources") from exc

    def _drift(self, manifest):
        changed = []
        for resource in manifest["resources"]:
            target = Path(resource["path"])
            current = _inventory(target)
            recorded = resource["entries"]
            for relative in sorted(set(current) | set(recorded)):
                if current.get(relative) != recorded.get(relative):
                    changed.append(str(target if relative == "." else target / relative))
        return changed

    def _source_fingerprint(self):
        data = {part: _inventory(self.source / part, source_exclusions=True) for part in SOURCE_PARTS}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _preflight(self, previous):
        self._safe_paths()
        self._check_state_paths()
        if previous:
            drift = self._drift(previous)
            if drift:
                raise InstallError("modified or added install resources are preserved; reinstall blocked:\n" + "\n".join(drift[:20]))
        if not previous:
            for target in [self.prefix / "managed", *[p / "ai-secretary" for p in self.skills_dirs]]:
                if _exists(target):
                    raise InstallError(f"refusing to overwrite an unowned resource: {target}")
        # Schema-1 installs owned only their private embedded sidecar. They may
        # migrate when clean, but can never adopt any existing registry file.
        if not previous or previous["schema_version"] == 1:
            for path in self._registry_paths():
                if _exists(path):
                    raise InstallError(f"refusing to overwrite an unowned registry sidecar: {path}")

    def _check_state_paths(self):
        state = self.prefix / "state"
        for path in [state, state / "secretary", state / "ghost-alice", state / "openclaw"]:
            if path.is_symlink() or (_exists(path) and not path.is_dir()):
                raise InstallError(f"private state path is not a real directory: {path}")
        for path in [state / "gateway.token", state / "openclaw.json", state / "secretary/runtime.json"]:
            if path.is_symlink() or (_exists(path) and not path.is_file()):
                raise InstallError(f"private state file is not a regular file: {path}")
        runtime = state / "secretary/runtime.json"
        if runtime.exists():
            self._private_file(runtime)

    def _prepare_state_directories(self):
        self._check_state_paths()
        state = self.prefix / "state"
        for directory in [state, state / "secretary", state / "ghost-alice", state / "openclaw"]:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _initialize_state(self, config_template, runtime_template):
        self._prepare_state_directories()
        state = self.prefix / "state"
        token_path = state / "gateway.token"
        if not token_path.exists():
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_hex(32) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        for config_path, template in ((state / "openclaw.json", config_template),
                                      (state / "secretary/runtime.json", runtime_template)):
            if not config_path.exists():
                fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Path(template).read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())

    def _packaged_backends(self):
        required = {"builtin", self.native_backend}
        configuration = self.prefix / "state/secretary/runtime.json"
        if configuration.exists():
            try:
                runtime = _json_object(configuration, "private runtime configuration")
            except (ValueError, OSError) as exc:
                raise InstallError("private runtime configuration cannot be verified; preserve its bytes for review") from exc
            selected = runtime.get("native_backend", "builtin")
            if selected not in ("builtin", "peekaboo"):
                raise InstallError("private runtime configuration selects an unsupported native backend; preserve it for review")
            required.add(selected)
        return sorted(required)

    def _provision_node(self, target):
        candidate = self.node_source
        if candidate is None:
            found = shutil.which("node")
            if found:
                executable = Path(found).resolve()
                if _run([executable, "--version"]) == "v" + NODE_VERSION:
                    root = executable.parent.parent
                    if (root / "lib/node_modules/npm/bin/npm-cli.js").is_file():
                        candidate = root
        if candidate is not None:
            if _run([candidate / "bin/node", "--version"]) != "v" + NODE_VERSION:
                raise InstallError("local Node distribution does not match Node " + NODE_VERSION)
            _copy(candidate, target)
            return
        system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
        architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(platform.machine())
        if not system or not architecture:
            raise InstallError("no supported private Node distribution for this platform")
        filename = f"node-v{NODE_VERSION}-{system}-{architecture}.tar.gz"
        base_url = f"https://nodejs.org/dist/v{NODE_VERSION}/"
        with tempfile.TemporaryDirectory(prefix="secretary-node-") as temporary:
            archive = Path(temporary) / filename
            try:
                with urllib.request.urlopen(base_url + "SHASUMS256.txt", timeout=60) as response:
                    checksums = response.read().decode()
                expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == filename)
                with urllib.request.urlopen(base_url + filename, timeout=60) as response, archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
            except (OSError, StopIteration) as exc:
                raise InstallError("private Node download failed") from exc
            if _hash(archive) != expected:
                raise InstallError("private Node archive checksum mismatch")
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(temporary, filter="data")
            _copy(Path(temporary) / filename.removesuffix(".tar.gz"), target)

    def _validate_openclaw(self, runtime):
        try:
            package = json.loads((runtime / "node_modules/openclaw/package.json").read_text())
            lock = json.loads((runtime / "package-lock.json").read_text())
            entries = [entry for key, entry in lock["packages"].items() if key.endswith("node_modules/openclaw")]
            if package.get("version") != OPENCLAW_VERSION or len(entries) != 1:
                raise InstallError("OpenClaw runtime does not match the exact pinned version")
            entry = entries[0]
            if entry.get("version") != OPENCLAW_VERSION or entry.get("integrity") != OPENCLAW_INTEGRITY:
                raise InstallError("OpenClaw package lock does not match pinned npm integrity")
            if not (runtime / "node_modules/openclaw/openclaw.mjs").is_file():
                raise InstallError("OpenClaw CLI entrypoint is missing")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InstallError("OpenClaw runtime or integrity metadata is missing") from exc

    def _provision_openclaw(self, node, target):
        if self.openclaw_source:
            self._validate_openclaw(self.openclaw_source)
            self._verify_cached_package(self.openclaw_source)
            _copy(self.openclaw_source, target)
        else:
            target.mkdir(parents=True)
            _json_write(target / "package.json", {"name": "ai-secretary-private-runtime", "private": True,
                "dependencies": {"openclaw": OPENCLAW_VERSION}})
            env = dict(os.environ)
            env["PATH"] = str(node / "bin") + os.pathsep + env.get("PATH", "")
            env["npm_config_cache"] = str(target / ".npm-cache")
            _run([node / "bin/node", node / "lib/node_modules/npm/bin/npm-cli.js", "install", "--prefix", target,
                "--ignore-scripts", "--no-audit", "--no-fund", "--save-exact", "openclaw@" + OPENCLAW_VERSION], env=env)
            shutil.rmtree(target / ".npm-cache", ignore_errors=True)
        self._validate_openclaw(target)
        # npm dependency lifecycle scripts remain disabled. The pinned
        # OpenClaw launcher owns a separate initialization marker; complete
        # that explicit package initialization inside staging so its first
        # user invocation cannot invalidate the ownership snapshot.
        bootstrap_env = dict(os.environ)
        bootstrap_env["PATH"] = str(node / "bin") + os.pathsep + bootstrap_env.get("PATH", "")
        bootstrap_env["OPENCLAW_STATE_DIR"] = str(target / ".bootstrap-state")
        bootstrap_env["OPENCLAW_CONFIG_PATH"] = str(target / ".bootstrap-state/openclaw.json")
        bootstrap_env["NODE_DISABLE_COMPILE_CACHE"] = "1"
        bootstrap_env["npm_config_cache"] = str(target / ".npm-cache")
        version = _run([node / "bin/node", target / "node_modules/openclaw/openclaw.mjs", "--version"], env=bootstrap_env)
        if OPENCLAW_VERSION not in version:
            raise InstallError("provisioned OpenClaw CLI did not report the pinned version")
        shutil.rmtree(target / ".bootstrap-state", ignore_errors=True)
        shutil.rmtree(target / ".npm-cache", ignore_errors=True)

    def _verify_cached_package(self, runtime):
        """Bind the cached installed package bytes to the pinned npm artifact.

        Lock metadata alone is not proof of package content. npm's content
        cache names tarballs by the integrity digest; verify that digest and
        compare the artifact's package members before trusting a cached prefix.
        """
        expected = base64.b64decode(OPENCLAW_INTEGRITY.split("-", 1)[1])
        content_key = expected.hex()
        cache_roots = [runtime.parent / "npm-cache", runtime / ".npm-cache", Path.home() / ".npm"]
        configured_cache = os.environ.get("npm_config_cache")
        if configured_cache:
            cache_roots.insert(0, Path(configured_cache).expanduser())
        candidates = [root / "_cacache/content-v2/sha512" / content_key[:2] / content_key[2:4] / content_key[4:]
            for root in cache_roots]
        archive = next((path for path in candidates if path.is_file()), None)
        if archive is None:
            raise InstallError("cached OpenClaw requires its pinned tarball in the sibling npm-cache or npm content cache")
        digest = hashlib.sha512()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.digest() != expected:
            raise InstallError("cached OpenClaw tarball integrity mismatch")
        package_root = runtime / "node_modules/openclaw"
        with tarfile.open(archive, "r:gz") as artifact:
            for member in artifact:
                relative = Path(member.name)
                if not relative.parts or relative.parts[0] != "package" or ".." in relative.parts:
                    raise InstallError("pinned OpenClaw artifact contains an unsafe package path")
                path = package_root.joinpath(*relative.parts[1:])
                # The package's own lifecycle contract consumes this marker
                # after initialization; its absence in an initialized cache
                # is expected and does not change executable package content.
                if member.name == "package/.openclaw-lifecycle-pending" and not _exists(path):
                    continue
                if member.isfile():
                    if path.is_symlink() or not path.is_file():
                        raise InstallError(f"cached OpenClaw package file is missing or replaced: {relative}")
                    source_digest = hashlib.sha256()
                    stream = artifact.extractfile(member)
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        source_digest.update(block)
                    if source_digest.hexdigest() != _hash(path):
                        raise InstallError(f"cached OpenClaw package file differs from pinned artifact: {relative}")
                elif member.issym():
                    if not path.is_symlink() or os.readlink(path) != member.linkname:
                        raise InstallError(f"cached OpenClaw package symlink differs from pinned artifact: {relative}")
                elif not member.isdir():
                    raise InstallError("pinned OpenClaw artifact contains an unsupported file type")

    def _launchers_and_config(self, managed):
        final = self.prefix / "managed"
        state = self.prefix / "state"
        python = str(Path(sys.executable).resolve())
        node = str(final / "node/bin/node")
        native = str(final / "app/build" / ("desktop-secretary-peekaboo" if self.native_backend == "peekaboo" else "desktop-secretary"))
        common = {
            "PYTHONPATH": str(final / "app/src"), "PYTHONDONTWRITEBYTECODE": "1",
            "SECRETARY_STATE_DIR": str(state / "secretary"),
            "SECRETARY_GA_LEDGER_ROOT": str(state / "ghost-alice"),
            "SECRETARY_NATIVE_BINARY": native,
            "SECRETARY_GHOST_ROOT": str(self.ghost_root), "SECRETARY_NODE": node,
            "SECRETARY_OPENCLAW_BINARY": str(final / "bin/openclaw"),
            "OPENCLAW_STATE_DIR": str(state / "openclaw"),
            "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
        }
        (managed / "bin").mkdir()
        exports = "\n".join("export " + key + "=" + shlex.quote(value) for key, value in common.items())
        exports += "\nexport PATH=" + shlex.quote(str(final / "node/bin")) + ':"$PATH"'
        exports += '\nexport OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-$(cat ' + shlex.quote(str(state / "gateway.token")) + ')}"'
        # The inherited shared descriptor protects every owned runtime process,
        # including a Gateway with no jobs and a CLI before authority bootstrap.
        lifetime = """import fcntl,os,stat,sys
parent=os.open(os.path.dirname(sys.argv[1]),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
metadata=os.fstat(parent)
if metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o700: raise RuntimeError('runtime authority directory must be owned and mode 700')
flags=os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK
try:
 fd=os.open(os.path.basename(sys.argv[1]),flags|os.O_CREAT|os.O_EXCL,0o600,dir_fd=parent)
 os.fchmod(fd,0o600)
except FileExistsError:
 fd=os.open(os.path.basename(sys.argv[1]),flags,dir_fd=parent)
metadata=os.fstat(fd)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o600: raise RuntimeError('runtime lifetime lock must be owned and mode 600')
fcntl.flock(fd,fcntl.LOCK_SH)
current=os.stat(os.path.basename(sys.argv[1]),dir_fd=parent,follow_symlinks=False)
if (metadata.st_dev,metadata.st_ino)!=(current.st_dev,current.st_ino): raise RuntimeError('runtime lifetime lock identity changed')
os.close(parent)
os.set_inheritable(fd,True)
os.execvpe(sys.argv[2],sys.argv[2:],os.environ)
"""
        for name, command in {
            "ai-secretary": [python, "-m", "ai_secretary.control"],
            "ai-secretary-worker": [python, "-m", "ai_secretary.service"],
            "ai-secretary-python": [python],
            "openclaw": [node, str(final / "openclaw/node_modules/openclaw/openclaw.mjs")],
        }.items():
            path = managed / "bin" / name
            guarded_command = [python, "-c", lifetime, str(state / "secretary/runtime-use.lock"), *command]
            path.write_text("#!/bin/sh\nset -eu\n" + exports + "\nexec " + shlex.join(guarded_command) + ' "$@"\n')
            path.chmod(0o755)
        plugin_config = {"python": python, "source": str(final / "app/src"),
            "nativeBinary": native, "stateDir": str(state / "secretary"),
            "workerExecutable": str(final / "bin/ai-secretary-worker"),
            "ghostRoot": str(self.ghost_root), "gaLedgerRoot": str(state / "ghost-alice"), "node": node}
        config = {"gateway": {"mode": "local", "bind": "loopback", "auth": {"mode": "token"}},
            "channels": {}, "logging": {"file": str(state / "openclaw/gateway.log")},
            "skills": {"load": {"extraDirs": [str(final / "app/addons/ai-secretary")]}, "workshop": {"autonomous": {"mode": "off"}}},
            "plugins": {"allow": ["ai-secretary"], "load": {"paths": [str(final / "app/openclaw-plugin")]},
                "slots": {"memory": "none"},
                "entries": {"ai-secretary": {"enabled": True, "config": plugin_config,
                    "hooks": {"allowConversationAccess": True, "allowPromptInjection": True}}}},
            "agents": {"defaults": {"model": {"primary": "ollama/qwen3-vl:8b"}, "heartbeat": {"every": "0m"}}},
            "update": {"checkOnStart": False},
            "models": {"catalogRefresh": {"enabled": False}, "providers": {"ollama": {"baseUrl": "http://127.0.0.1:11434", "apiKey": "ollama-local", "api": "ollama",
                "models": [{"id": "qwen3-vl:8b", "name": "Qwen3 VL 8B", "input": ["text", "image"], "reasoning": False,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": 32768, "maxTokens": 8192}]}}},
            "tools": {"allow": ["secretary_*"], "deny": ["exec", "process", "read", "write", "edit", "apply_patch", "browser", "message"],
                "media": {kind: {"enabled": False} for kind in ("image", "audio", "video")}}}
        _json_write(managed / "config/openclaw.json", config)
        _json_write(managed / "config/runtime.json", {"native_backend": self.native_backend, "native_binary": native,
            "python_executable": str(final / "bin/ai-secretary-python"), "openclaw_executable": str(final / "bin/openclaw"),
            "openclaw_state": str(state / "openclaw"), "openclaw_config": str(state / "openclaw.json")})

    def _publish(self, targets, work_root):
        # The generic skill transaction excludes node_modules. Our managed
        # runtime is explicitly dependency-bearing, so scope that copy policy
        # to this batch while reusing its staging/verification/rollback path.
        with _FRAMEWORK_PUBLISH_LOCK:
            self._publish_locked(targets, work_root)

    def _publish_locked(self, targets, work_root):
        framework = self.framework()
        journal = {"schema_version": 1, "prefix": str(self.prefix), "registry_dir": str(self.registry_dir),
            "platform_registries": self._registry_map(), "work_root": str(work_root),
            "work_root_hash": _tree_identity(work_root), "moves": []}
        _json_write(self.journal_path, journal)
        original = framework.transaction._rename_path
        original_remove = framework.transaction._remove_path
        original_excludes = framework.transaction.EXCLUDED_COPY_DIR_NAMES
        previous_inventory = [(Path(dest), _inventory(dest)) for _, dest in targets]
        published_hashes = {}
        target_paths = {str(dest) for _, dest in targets}
        def journaled_rename(source, dest):
            source_hash = _tree_identity(source)
            journal["moves"].append({"source": str(source), "dest": str(dest), "source_hash": source_hash})
            _json_write(self.journal_path, journal)
            if str(dest) in target_paths and Path(source).parent.name == ".ghost-alice-install-staging":
                published_hashes[str(dest)] = source_hash
            original(source, dest)
        def ownership_checked_remove(path):
            expected = published_hashes.get(str(path))
            if expected is not None and _tree_identity(path) != expected:
                raise OSError(f"published resource changed; preserve it instead of rollback deletion: {path}")
            original_remove(path)
        framework.transaction._rename_path = journaled_rename
        framework.transaction._remove_path = ownership_checked_remove
        framework.transaction.EXCLUDED_COPY_DIR_NAMES = original_excludes - {"node_modules"}
        try:
            framework.transaction.staged_copy_replace_many(targets,
                rollback_root=self.prefix.parent / ("." + self.prefix.name + ".rollbacks"), rollback_keep=2**63 - 1)
        except Exception as exc:
            # Retain recovery information if the framework could not restore
            # every previous target; never erase an uncertain transaction.
            if all(_inventory(dest) == before for dest, before in previous_inventory):
                self._cleanup_transaction_paths(journal["moves"])
                self.journal_path.unlink(missing_ok=True)
            raise InstallError(f"installation transaction failed: {exc}") from exc
        else:
            journal["committed"] = True
            _json_write(self.journal_path, journal)
            self._cleanup_transaction_paths(journal["moves"])
            self.journal_path.unlink()
        finally:
            framework.transaction._rename_path = original
            framework.transaction._remove_path = original_remove
            framework.transaction.EXCLUDED_COPY_DIR_NAMES = original_excludes

    def install(self):
        if platform.system() != "Darwin":
            raise InstallError("AI Secretary desktop bundle installation requires macOS (Darwin)")
        with self._lock():
            previous = self._manifest()
            self._preflight(previous)
            framework = self.framework()
            try:
                targets = framework.addon.load_addon_targets([self.source], platform=next(iter(self._registries())))
            except framework.addon.AddonManifestError as exc:
                raise InstallError(str(exc)) from exc
            if len(targets) != 1 or targets[0].addon_id != "ai-secretary" or targets[0].name != "ai-secretary":
                raise InstallError("source must declare exactly the ai-secretary addon skill")
            fingerprint = self._source_fingerprint()
            installer_fingerprint = _hash(Path(__file__))
            packaged_backends = self._packaged_backends()
            if (previous and previous["schema_version"] == SCHEMA_VERSION and previous.get("source_fingerprint") == fingerprint
                    and previous.get("installer_fingerprint") == installer_fingerprint
                    and previous.get("source") == str(self.source)
                    and previous.get("ghost_root") == str(self.ghost_root)
                    and previous.get("node_version") == NODE_VERSION
                    and previous.get("openclaw_version") == OPENCLAW_VERSION
                    and previous.get("openclaw_integrity") == OPENCLAW_INTEGRITY
                    and previous.get("native_backend", "builtin") == self.native_backend
                    and previous.get("packaged_backends", ["builtin"]) == packaged_backends):
                needed_state = (self.prefix / "state/gateway.token", self.prefix / "state/openclaw.json", self.prefix / "state/secretary/runtime.json")
                if not all(path.exists() for path in needed_state):
                    self._prepare_state_directories()
                    with self._runtime_guard():
                        self._initialize_state(self.prefix / "managed/config/openclaw.json", self.prefix / "managed/config/runtime.json")
                return {"status": "unchanged", "prefix": str(self.prefix), "openclaw_version": OPENCLAW_VERSION,
                    "registry_dir": str(self.registry_dir)}
            self.prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
            work_root = Path(tempfile.mkdtemp(prefix="." + self.prefix.name + ".stage-", dir=self.prefix.parent))
            try:
                managed = work_root / "managed"
                managed.mkdir()
                self._provision_node(managed / "node")
                self._provision_openclaw(managed / "node", managed / "openclaw")
                app = managed / "app"
                app.mkdir()
                for part in SOURCE_PARTS:
                    _copy(self.source / part, app / part)
                _run(["/bin/sh", app / "scripts/build_native.sh", "--backend", "all" if "peekaboo" in packaged_backends else "builtin",
                    "--output-dir", app / "build", "--scratch-path", work_root / "native-build", "--cache-path", work_root / "native-cache"], cwd=app)
                if not (app / "build/desktop-secretary").is_file():
                    raise InstallError("native build did not produce build/desktop-secretary")
                if "peekaboo" in packaged_backends and not (app / "build/desktop-secretary-peekaboo").is_file():
                    raise InstallError("selected peekaboo native build did not produce build/desktop-secretary-peekaboo; no backend fallback was published")
                shutil.rmtree(app / "build/TestWindow.app", ignore_errors=True)
                shutil.rmtree(work_root / "native-build", ignore_errors=True)
                shutil.rmtree(work_root / "native-cache", ignore_errors=True)
                legacy_plugin_alias = bool(previous and (previous["schema_version"] == 1 or previous.get("legacy_plugin_alias")))
                if legacy_plugin_alias:
                    # Preserve old mutable config references without keeping a
                    # second plugin copy. Node resolves module files physically.
                    (managed / "plugin").symlink_to("app/openclaw-plugin", target_is_directory=True)
                self._launchers_and_config(managed)
                skill_sources = []
                provided = []
                for index, directory in enumerate(self.skills_dirs):
                    skill = work_root / f"skill-{index}"
                    _copy(targets[0].source, skill)
                    skill_sources.append((skill, directory / "ai-secretary"))
                    provided.append({"kind": "skill", "name": "ai-secretary", "target": str(directory / "ai-secretary"),
                        "ownership": "addon", "install_mode": "copy", "content_hash": framework.addon.hash_utils.hash_target(skill, "copy"),
                        "marker": "ai-secretary", "metadata": {"private_prefix": str(self.prefix)}})
                uninstaller = [str(Path(sys.executable).resolve()), str(self.prefix / "managed/app/scripts/installer.py"),
                    "uninstall", "--prefix", str(self.prefix)]
                if self._typed_registries:
                    for registry_platform, directory in self._registries().items():
                        uninstaller += ["--platform-registry", registry_platform + "=" + str(directory)]
                else:
                    uninstaller += ["--registry-dir", str(self.registry_dir)]
                for directory in self.skills_dirs:
                    uninstaller += ["--skills-dir", str(directory)]
                uninstaller += ["--ghost-alice-root", str(self.ghost_root)]
                provided.append({"kind": "resource", "name": "ai-secretary-runtime", "target": str(self.prefix / "managed"),
                    "ownership": "addon", "install_mode": "copy", "content_hash": framework.addon.hash_utils.hash_target(managed, "copy"),
                    "marker": "ai-secretary", "metadata": {"private_prefix": str(self.prefix),
                        "entrypoints": {name: str(self.prefix / "managed/bin" / name) for name in ("ai-secretary", "ai-secretary-worker", "ai-secretary-python", "openclaw")},
                        "uninstaller": uninstaller, "removal_policy": "ai-secretary-owned-manifest",
                        "ownership_inventory": str(self.manifest_path)}})
                installed_at = datetime.now(timezone.utc).isoformat()
                sidecar_sources = []
                for registry_platform, directory in self._registries().items():
                    sidecar = framework.addon.build_sidecar_record(targets[0], platform=registry_platform, installed_at=installed_at, provided=provided)
                    sidecar["installation"] = {"prefix": str(self.prefix), "registry_dir": str(directory),
                        "platform_registries": self._registry_map(), "installer": "ai-secretary", "distribution": "first-party"}
                    staged_sidecar = framework.registry.write_record(sidecar, addons_dir=work_root / "registry" / registry_platform)
                    sidecar_sources.append((registry_platform, staged_sidecar, directory / "ai-secretary.json"))
                resources = [{"role": "runtime", "path": str(self.prefix / "managed"), "entries": _inventory(managed)}]
                resources += [{"role": "skill", "path": str(dest), "entries": _inventory(src)} for src, dest in skill_sources]
                resources += [{"role": "registry", "platform": registry_platform, "path": str(dest), "entries": _inventory(src)}
                    for registry_platform, src, dest in sidecar_sources]
                manifest = {"schema_version": SCHEMA_VERSION, "prefix": str(self.prefix), "install_id": uuid.uuid4().hex,
                    "source": str(self.source), "source_fingerprint": fingerprint, "installed_at": installed_at,
                    "installer_fingerprint": installer_fingerprint, "ghost_root": str(self.ghost_root),
                    "openclaw_version": OPENCLAW_VERSION, "openclaw_integrity": OPENCLAW_INTEGRITY,
                    "node_version": NODE_VERSION, "resources": resources, "state_dir": str(self.prefix / "state"),
                    "registry_dir": str(self.registry_dir), "legacy_plugin_alias": legacy_plugin_alias,
                    "native_backend": self.native_backend, "packaged_backends": packaged_backends}
                if self._typed_registries:
                    manifest["platform_registries"] = self._registry_map()
                staged_manifest = work_root / "install-manifest.json"
                _json_write(staged_manifest, manifest)
                # Recheck ownership immediately before replacing resources.
                if self._source_fingerprint() != fingerprint:
                    raise InstallError("source changed during staging; previous install is retained, retry from stable sources")
                self._prepare_state_directories()
                with self._runtime_guard():
                    self._preflight(previous)
                    if self._packaged_backends() != packaged_backends:
                        raise InstallError("native runtime selection changed during staging; preserve the current install and retry")
                    self._initialize_state(managed / "config/openclaw.json", managed / "config/runtime.json")
                    self._publish([(managed, self.prefix / "managed"), *skill_sources,
                        *[(src, dest) for _, src, dest in sidecar_sources], (staged_manifest, self.manifest_path)], work_root)
                return {"status": "installed", "prefix": str(self.prefix), "openclaw_version": OPENCLAW_VERSION,
                    "bin": str(self.prefix / "managed/bin"), "state_dir": str(self.prefix / "state"), "registry_dir": str(self.registry_dir)}
            finally:
                if not self.journal_path.exists():
                    shutil.rmtree(work_root, ignore_errors=True)

    def status(self):
        with self._lock():
            manifest = self._manifest()
            if not manifest:
                return {"status": "not-installed", "prefix": str(self.prefix)}
            drift = self._drift(manifest)
            return {"status": "drifted" if drift else "installed", "prefix": str(self.prefix), "drift": drift,
                "openclaw_version": manifest["openclaw_version"], "node_version": manifest["node_version"],
                "registry_dir": manifest.get("registry_dir"),
                "platform_registries": manifest.get("platform_registries", {"codex": manifest.get("registry_dir")}),
                "registry_status": "registered" if manifest["schema_version"] == SCHEMA_VERSION and not any(str(path) in drift for path in self._registry_paths()) else "legacy-unregistered" if manifest["schema_version"] == 1 else "drifted",
                "state_dir": str(self.prefix / "state"), "model_status": "not-checked", "accessibility_status": "not-checked"}

    def uninstall(self):
        with self._lock(runtime_guard=True):
            manifest = self._manifest()
            if not manifest:
                return {"status": "not-installed", "preserved": [], "prefix": str(self.prefix)}
            # A preserved, valid sidecar remains discoverable. Never remove
            # its runtime or skill while attribution has user changes: keep the
            # entire installation and ownership manifest available for review.
            registry_resources = [resource for resource in manifest["resources"] if resource["role"] == "registry"]
            for resource in registry_resources:
                if _exists(resource["path"]) and _inventory(resource["path"]) != resource["entries"]:
                    raise InstallError(f"modified registry sidecar is preserved; uninstall blocked: {resource['path']}")
            preserved = []
            # Remove clean attribution first, so an unexpected removal failure
            # cannot leave a discoverable record after deleting its entrypoints.
            # Keep the recorded uninstaller and its parent directories until
            # all other runtime/skill resources have been processed. A partial
            # removal can then retry through that same installed command.
            primary_resources, deferred_resources = [], []
            deferred_keys = {".", "app", "app/scripts", "app/scripts/installer.py"}
            for resource in manifest["resources"]:
                if resource["role"] == "registry":
                    continue
                if resource["role"] == "runtime" and "app/scripts/installer.py" in resource["entries"]:
                    primary_resources.append({**resource, "entries": {key: value for key, value in resource["entries"].items() if key not in deferred_keys}})
                    deferred_resources.append({**resource, "entries": {key: value for key, value in resource["entries"].items() if key in deferred_keys}})
                else:
                    primary_resources.append(resource)
            def remove_resources(resources):
                for resource in resources:
                    root = Path(resource["path"])
                    for relative, owned in sorted(resource["entries"].items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
                        path = root if relative == "." else root / relative
                        # Do not walk through a replaced parent symlink.
                        parents = [parent for parent in path.parents if parent == root or root in parent.parents]
                        if any(parent.is_symlink() for parent in parents):
                            preserved.append(str(path))
                            continue
                        if not _exists(path):
                            continue
                        if _entry(path) != owned:
                            if resource["role"] == "registry":
                                raise InstallError(f"registry sidecar changed during uninstall; uninstall blocked: {path}")
                            preserved.append(str(path))
                            continue
                        if owned["kind"] == "directory":
                            try:
                                path.rmdir()
                            except OSError:
                                pass  # Unowned files or modified files remain inside.
                        else:
                            path.unlink()
            remove_resources(registry_resources + primary_resources)
            for resource in manifest["resources"]:
                root = Path(resource["path"])
                if _exists(root):
                    for relative, current in _inventory(root).items():
                        path = root if relative == "." else root / relative
                        deferred = next((item["entries"].get(relative) for item in deferred_resources if item["path"] == resource["path"]), None)
                        if (path.is_file() or path.is_symlink()) and current != deferred:
                            preserved.append(str(path))
            # Final bookkeeping must succeed before consuming the retry
            # command. If its own unlink fails, restore the manifest so that
            # the still-present command can retry with its ownership proof.
            self.manifest_path.unlink()
            try:
                remove_resources(deferred_resources)
            except BaseException:
                _json_write(self.manifest_path, manifest)
                raise
            return {"status": "preserved" if preserved else "uninstalled", "prefix": str(self.prefix),
                "preserved": sorted(set(preserved)), "state_dir": str(self.prefix / "state")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "status", "uninstall"))
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/ai-secretary")
    parser.add_argument("--skills-dir", action="append", type=Path)
    parser.add_argument("--registry-dir", type=Path,
        help="Ghost-ALICE addon registry directory (persisted in the installation manifest)")
    parser.add_argument("--platform-registry", action="append", default=[], metavar="PLATFORM=DIR",
        help="own a platform sidecar in an absolute registry directory; repeat for codex and claude")
    parser.add_argument("--claude", action="store_true", help="also copy the skill to ~/.claude/skills")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ghost-alice-root", type=Path, default=Path(os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice"))))
    parser.add_argument("--openclaw-source", type=Path, help="existing npm prefix containing the exact pinned package and package-lock.json")
    parser.add_argument("--node-source", type=Path, help="local Node 24.21.0 distribution root")
    parser.add_argument("--native-backend", choices=("builtin", "peekaboo"), default="builtin",
        help="compile the selected backend explicitly; peekaboo also retains the independent builtin product")
    args = parser.parse_args(argv)
    skills = args.skills_dir or [Path.home() / ".agents/skills"]
    if args.claude:
        skills.append(Path.home() / ".claude/skills")
    try:
        platform_registries = None
        if args.platform_registry:
            if args.registry_dir is not None:
                raise InstallError("--registry-dir and --platform-registry cannot be mixed")
            platform_registries = {}
            for selection in args.platform_registry:
                registry_platform, separator, directory = selection.partition("=")
                if not separator or registry_platform not in REGISTRY_PLATFORMS or not directory:
                    raise InstallError("--platform-registry requires codex=/absolute/dir or claude=/absolute/dir")
                if registry_platform in platform_registries:
                    raise InstallError("duplicate platform registry key: " + registry_platform)
                platform_registries[registry_platform] = Path(directory)
        registry_dir = args.registry_dir if args.registry_dir is not None else None if platform_registries is not None else Path.home() / ".ghost-alice/addons"
        installer = Installer(args.prefix, skills, args.source, args.ghost_alice_root,
            registry_dir=registry_dir, platform_registries=platform_registries,
            openclaw_source=args.openclaw_source, node_source=args.node_source, native_backend=args.native_backend)
        result = getattr(installer, args.command)()
    except (InstallError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
