"""SQLite authority for effect replay, exact drafts and fenced desktop access.

Operation recovery is intentionally explicit. The caller must hold its exclusive
worker lock before recover_inflight(), because recovery invalidates live attempts.
Approval actor authentication belongs to the local user control layer; this
storage API is never an approval tool exposed to the model.
"""

from contextlib import contextmanager
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from .contracts import (
    ApprovalRequired, BindingConflict, EffectSpec, LeaseBusy,
    canonical_json, content_digest, identifier,
)


@lru_cache(maxsize=1)
def _boot_id() -> str:
    """Identify the boot that supplies time.monotonic() across processes."""
    if sys.platform == "darwin":
        completed = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                                   capture_output=True, text=True, timeout=2, check=True)
        value = completed.stdout.strip()
    elif sys.platform.startswith("linux"):
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    else:
        raise RuntimeError("Cannot establish host boot identity on this platform")
    if not value:
        raise RuntimeError("Host boot identity is unavailable")
    return value


class Ledger:
    """File-backed ledger; each call uses its own bounded SQLite connection."""

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("Ledger requires a durable filesystem path")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Another first-open worker may have won creation; preserve its file.
            pass
        else:
            os.close(fd)
        with self._connection() as connection:
            # Journal-mode negotiation may report SQLITE_BUSY immediately during
            # simultaneous first opens, even with the connection busy timeout.
            deadline = time.monotonic() + 10
            while True:
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 0xff
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, intent_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), owner TEXT NOT NULL,
                    effect_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('Prepared','Applying','Applied','Failed','Unknown')),
                    attempt_id TEXT, result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    draft_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    content_json TEXT NOT NULL, content_digest TEXT NOT NULL,
                    approved_digest TEXT, approved_actor TEXT
                );
                CREATE TABLE IF NOT EXISTS lease_counter (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), fence INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO lease_counter(singleton,fence) VALUES (1,0);
                CREATE TABLE IF NOT EXISTS desktop_lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL,
                    fence INTEGER NOT NULL, boot_id TEXT NOT NULL, expires REAL NOT NULL
                );
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def create_task(self, task_id, owner, intent):
        identifier(task_id, "task_id")
        identifier(owner, "owner")
        intent_json = canonical_json(intent)
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                if existing["owner"] != owner or existing["intent_json"] != intent_json:
                    raise BindingConflict("Task identifier is bound to another owner or intent")
                return
            connection.execute("INSERT INTO tasks VALUES (?,?,?)", (task_id, owner, intent_json))

    def prepare(self, operation_id, spec):
        identifier(operation_id, "operation_id")
        if not isinstance(spec, EffectSpec):
            raise TypeError("prepare requires an EffectSpec")
        with self._transaction() as connection:
            task = connection.execute("SELECT owner FROM tasks WHERE task_id=?", (spec.task_id,)).fetchone()
            if task is None:
                raise BindingConflict("Effect refers to an unknown task")
            existing = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if (existing["task_id"] != spec.task_id or existing["owner"] != task["owner"]
                        or existing["effect_json"] != spec.canonical):
                    raise BindingConflict("Operation identifier is bound to another final effect")
                return {"state": existing["state"], "fingerprint": existing["fingerprint"]}
            connection.execute("""INSERT INTO operations
                (operation_id,task_id,owner,effect_json,fingerprint,state) VALUES (?,?,?,?,?,'Prepared')""",
                (operation_id, spec.task_id, task["owner"], spec.canonical, spec.fingerprint))
            return {"state": "Prepared", "fingerprint": spec.fingerprint}

    def claim(self, operation_id):
        with self._transaction() as connection:
            operation = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if operation is None:
                raise BindingConflict("Operation must be prepared before claim")
            if operation["state"] == "Applied":
                return {"state": "cached", "result": json.loads(operation["result_json"])}
            if operation["state"] in ("Applying", "Unknown"):
                return {"state": "reconcile"}
            attempt = str(uuid.uuid4())
            connection.execute("""UPDATE operations SET state='Applying', attempt_id=?, result_json=NULL
                WHERE operation_id=?""", (attempt, operation_id))
            return {"state": "claimed", "attempt_id": attempt}

    def finish(self, operation_id, attempt_id, status, result):
        if not isinstance(status, str) or status.casefold() not in ("applied", "failed", "unknown"):
            raise ValueError("finish status must be Applied, Failed or Unknown")
        status = status.capitalize()
        result_json = canonical_json(result)
        if status == "Failed" and not (type(result) is dict and result.get("no_effect") is True):
            status = "Unknown"
        with self._transaction() as connection:
            operation = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if operation is None or operation["attempt_id"] != attempt_id:
                raise BindingConflict("Result does not belong to the current attempt")
            if operation["state"] != "Applying":
                if operation["state"] == status and operation["result_json"] == result_json:
                    return {"state": status, "result": result}
                raise BindingConflict("Attempt is no longer in flight")
            connection.execute("UPDATE operations SET state=?, result_json=? WHERE operation_id=?",
                               (status, result_json, operation_id))
            return {"state": status, "result": result}

    def recover_inflight(self):
        """Invalidate interrupted attempts only while caller holds exclusive worker lock."""
        with self._transaction() as connection:
            return connection.execute("UPDATE operations SET state='Unknown' WHERE state='Applying'").rowcount

    def create_draft(self, task_id, draft_id, content):
        identifier(draft_id, "draft_id")
        serialized = canonical_json(content)
        digest = content_digest(content)
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise BindingConflict("Draft refers to an unknown task")
            existing = connection.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if existing:
                if existing["task_id"] != task_id:
                    raise BindingConflict("Draft identifier is bound to another task")
                if existing["content_digest"] != digest:
                    connection.execute("""UPDATE drafts SET content_json=?, content_digest=?,
                        approved_digest=NULL, approved_actor=NULL WHERE draft_id=?""", (serialized, digest, draft_id))
            else:
                connection.execute("INSERT INTO drafts(draft_id,task_id,content_json,content_digest) VALUES (?,?,?,?)",
                                   (draft_id, task_id, serialized, digest))
        return digest

    def approve(self, draft_id, content_digest, actor):
        """Record an authenticated actor supplied exclusively by local user control."""
        if type(actor) is not str or not actor.strip():
            raise ApprovalRequired("An authenticated user actor is required")
        with self._transaction() as connection:
            draft = connection.execute("SELECT content_digest FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if draft is None or draft["content_digest"] != content_digest:
                raise ApprovalRequired("Approval must name the current exact content digest")
            connection.execute("UPDATE drafts SET approved_digest=?, approved_actor=? WHERE draft_id=?",
                               (content_digest, actor, draft_id))

    def validate_approval(self, draft_id, content, task_id=None):
        digest = content_digest(content)
        with self._connection() as connection:
            draft = connection.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if (draft is None or draft["content_digest"] != digest or draft["approved_digest"] != digest
                    or not draft["approved_actor"] or (task_id is not None and draft["task_id"] != task_id)):
                raise ApprovalRequired("The exact current outgoing content requires user approval")
        return True

    def acquire_lease(self, owner, ttl=30):
        identifier(owner, "owner")
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease ttl must be a positive finite number")
        boot = _boot_id()
        with self._transaction() as connection:
            now = time.monotonic()
            lease = connection.execute("SELECT * FROM desktop_lease WHERE singleton=1").fetchone()
            if lease and lease["boot_id"] == boot and lease["expires"] > now:
                raise LeaseBusy("Desktop is leased by an active worker")
            connection.execute("UPDATE lease_counter SET fence=fence+1 WHERE singleton=1")
            fence = connection.execute("SELECT fence FROM lease_counter WHERE singleton=1").fetchone()[0]
            connection.execute("INSERT OR REPLACE INTO desktop_lease VALUES (1,?,?,?,?)",
                               (owner, fence, boot, now + ttl))
            return fence

    def assert_lease(self, owner, fence):
        if type(fence) is not int or fence <= 0:
            raise LeaseBusy("Desktop lease fence must be a positive integer")
        boot = _boot_id()
        with self._connection() as connection:
            lease = connection.execute("SELECT * FROM desktop_lease WHERE singleton=1").fetchone()
            if (lease is None or lease["owner"] != owner or lease["fence"] != fence
                    or lease["boot_id"] != boot or lease["expires"] <= time.monotonic()):
                raise LeaseBusy("Desktop lease is absent, expired or fenced out")
        return True

    def release_lease(self, owner, fence):
        if type(fence) is not int or fence <= 0:
            raise LeaseBusy("Desktop lease fence must be a positive integer")
        boot = _boot_id()
        with self._transaction() as connection:
            lease = connection.execute("SELECT * FROM desktop_lease WHERE singleton=1").fetchone()
            if (lease is None or lease["owner"] != owner or lease["fence"] != fence or lease["boot_id"] != boot):
                raise LeaseBusy("Cannot release another lease generation")
            connection.execute("DELETE FROM desktop_lease WHERE singleton=1")
