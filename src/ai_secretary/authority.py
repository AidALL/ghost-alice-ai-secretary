"""One durable connection owner for composable local authority repositories.

Repositories receive an explicit UnitOfWork and must not open transactions or do
external IO. Governance, boot discovery and clock sampling happen before entry.
The existing operations file and its records remain the authority after migration.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import sqlite3
import time


_ACTIVE_PATHS = ContextVar("ai_secretary_authority_transactions", default=frozenset())

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, intent_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id), owner TEXT NOT NULL,
        effect_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('Prepared','Applying','Applied','Failed','Unknown')),
        attempt_id TEXT, result_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS drafts (
        draft_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        content_json TEXT NOT NULL, content_digest TEXT NOT NULL,
        approved_digest TEXT, approved_actor TEXT)""",
    """CREATE TABLE IF NOT EXISTS lease_counter (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), fence INTEGER NOT NULL)""",
    "INSERT OR IGNORE INTO lease_counter(singleton,fence) VALUES (1,0)",
    """CREATE TABLE IF NOT EXISTS desktop_lease (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL,
        fence INTEGER NOT NULL, boot_id TEXT NOT NULL, expires REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS authority_schema_version (
        version INTEGER PRIMARY KEY CHECK(version>0))""",
    """CREATE TABLE IF NOT EXISTS execution_lifecycle (
        lifecycle_id TEXT PRIMARY KEY,
        epoch INTEGER NOT NULL CHECK(epoch>=0),
        enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
        status TEXT NOT NULL CHECK(status IN('Stopped','Running','Draining')))""",
    "INSERT OR IGNORE INTO execution_lifecycle VALUES('scheduler',0,0,'Stopped')",
    """CREATE TABLE IF NOT EXISTS execution_contexts (
        run_id TEXT PRIMARY KEY,
        session TEXT NOT NULL, admission_id TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(task_id), input_event_id TEXT NOT NULL,
        lifecycle_id TEXT NOT NULL REFERENCES execution_lifecycle(lifecycle_id),
        epoch INTEGER NOT NULL CHECK(epoch>0), run_fence INTEGER NOT NULL CHECK(run_fence>0),
        desktop_fence INTEGER CHECK(desktop_fence>0),
        state TEXT NOT NULL CHECK(state IN('Active','Draining','Closed','Unknown')))""",
    """CREATE TABLE IF NOT EXISTS execution_operations (
        operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
        run_id TEXT NOT NULL REFERENCES execution_contexts(run_id), attempt_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS source_guard (
        source_id TEXT PRIMARY KEY,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN(0,1)),
        current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision>0),
        active_run_id TEXT, run_fence INTEGER NOT NULL DEFAULT 0 CHECK(run_fence>=0),
        unresolved_retrieval_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS authority_sources (
        source_id TEXT NOT NULL REFERENCES source_guard(source_id),
        revision INTEGER NOT NULL CHECK(revision>0),
        kind TEXT NOT NULL CHECK(kind IN('local_files','desktop_attachments')),
        scope_json TEXT NOT NULL, scope_digest TEXT NOT NULL,
        PRIMARY KEY(source_id,revision))""",
    """CREATE TABLE IF NOT EXISTS collection_grants (
        grant_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
        source_revision INTEGER NOT NULL CHECK(source_revision>0),
        session TEXT NOT NULL, admission_id TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(task_id), input_event_id TEXT NOT NULL,
        scope_digest TEXT NOT NULL, recipe TEXT NOT NULL, expires_at REAL NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN(0,1)),
        FOREIGN KEY(source_id,source_revision) REFERENCES authority_sources(source_id,revision))""",
    """CREATE TABLE IF NOT EXISTS authority_runs (
        run_id TEXT PRIMARY KEY REFERENCES execution_contexts(run_id),
        grant_id TEXT NOT NULL REFERENCES collection_grants(grant_id),
        source_id TEXT NOT NULL REFERENCES source_guard(source_id),
        source_revision INTEGER NOT NULL CHECK(source_revision>0),
        job_id TEXT, job_revision INTEGER CHECK(job_revision>0), occurrence_id TEXT UNIQUE,
        lifecycle_id TEXT NOT NULL REFERENCES execution_lifecycle(lifecycle_id),
        epoch INTEGER NOT NULL CHECK(epoch>0), run_fence INTEGER NOT NULL CHECK(run_fence>0),
        state TEXT NOT NULL,
        CHECK((job_id IS NULL AND job_revision IS NULL AND occurrence_id IS NULL)
           OR (job_id IS NOT NULL AND job_revision IS NOT NULL AND occurrence_id IS NOT NULL)))""",
    """CREATE TABLE IF NOT EXISTS authority_retrievals (
        retrieval_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES source_guard(source_id), item_id TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES authority_runs(run_id),
        operation_id TEXT UNIQUE REFERENCES operations(operation_id),
        state TEXT NOT NULL CHECK(state IN('Prepared','Applying','Pending','Unknown','Verified','NoEffect')),
        UNIQUE(source_id,item_id))""",
    "INSERT OR IGNORE INTO authority_schema_version(version) VALUES(1)",
)


class UnitOfWork:
    """A connection valid only during its owner's bounded database context."""

    def __init__(self, connection: sqlite3.Connection, *, read_only=False):
        if not read_only and not connection.in_transaction:
            raise RuntimeError("A writable unit requires an active transaction")
        self._connection = connection
        self._active = True
        self.read_only = read_only

    @property
    def active(self):
        return self._active

    @property
    def connection(self):
        if not self._active:
            raise RuntimeError("unit of work has ended")
        if not self.read_only and not self._connection.in_transaction:
            raise RuntimeError("unit of work transaction has ended")
        return self._connection

    def _invalidate(self):
        self._active = False


class AuthorityStore:
    """Own the existing private operations.sqlite and its additive migrations."""

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("AuthorityStore requires a durable filesystem path")
        self.path = Path(path).expanduser().resolve()
        self._reject_nested()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # A simultaneous first-open worker may already own the private file.
            pass
        else:
            os.close(fd)
        self._initialize()

    def _reject_nested(self):
        if self.path in _ACTIVE_PATHS.get():
            raise RuntimeError("nested authority transactions are not allowed; pass the existing unit")

    @contextmanager
    def _connection(self, *, read_only=False):
        target = self.path.as_uri() + "?mode=ro" if read_only else self.path
        db = sqlite3.connect(target, timeout=10, isolation_level=None, uri=read_only)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if read_only:
                db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    def _initialize(self):
        with self._connection() as db:
            # WAL negotiation can return SQLITE_BUSY before honoring busy_timeout.
            deadline = time.monotonic() + 10
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 0xff
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            db.execute("BEGIN IMMEDIATE")
            try:
                # executescript would implicitly commit an existing transaction.
                for statement in _SCHEMA:
                    db.execute(statement)
                if db.execute("SELECT max(version) FROM authority_schema_version").fetchone()[0] != 1:
                    raise RuntimeError("Authority schema is newer than this implementation")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self):
        self._reject_nested()
        token = _ACTIVE_PATHS.set(_ACTIVE_PATHS.get() | {self.path})
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                unit = UnitOfWork(db)
                try:
                    yield unit
                    if not db.in_transaction:
                        raise RuntimeError("unit of work transaction was ended by its caller")
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
                finally:
                    unit._invalidate()
        finally:
            _ACTIVE_PATHS.reset(token)

    @contextmanager
    def read(self):
        """Yield a bounded read-only connection, with no write transaction."""
        self._reject_nested()
        with self._connection(read_only=True) as db:
            yield db

    @contextmanager
    def _read_unit(self):
        """Adapt compatibility repository reads without opening another connection."""
        with self.read() as db:
            unit = UnitOfWork(db, read_only=True)
            try:
                yield unit
            finally:
                unit._invalidate()
