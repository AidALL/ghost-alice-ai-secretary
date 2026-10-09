"""Host-private worker capabilities and the model's bounded tool dispatch role.

Only a trusted host calls open/resolve/close with transport metadata. A bearer
capability binds that host's worker to stored authority; it does not authenticate
a human or protect against an unrestricted same-UID state editor or shell.
Neither the capability nor its transport metadata belongs in model tool data.
"""

from dataclasses import dataclass, field
import hashlib
import json
import math
import secrets
import time

from .authority_contracts import ExecutionContext, RunContext
from .contracts import canonical_json, identifier
from .coordinator import AuthorityDenied, AuthorityRepository, LifecycleTx
from .desktop_owner import DesktopOwnership


RESERVED_ARGUMENTS = frozenset({
    "session", "admission_id", "task_id", "input_event_id", "inputEventId",
    "lifecycle_id", "epoch", "run_id", "run_fence", "desktop_fence", "grant_id",
    "job_id", "job_revision", "occurrence_id", "owner", "ownership",
    "_authorization", "key_path", "backend", "kind", "origin", "actor",
    "source_revision", "token", "worker_token", "transport_id",
})


def _positive_number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a positive finite number")
    return float(value)


def _catalog(allowed_tools):
    if allowed_tools is None:
        # Runtime import permits the service to import this dispatch boundary.
        from .service import TOOL_NAMES
        allowed_tools = TOOL_NAMES
    return frozenset(allowed_tools)


def validate_worker_call(tool, args, *, allowed_tools=None):
    """Reject role/authority spoofing without gates, storage or native IO.

    A host can run this before resolving its private envelope. Reserved fields
    are top-level controls only: a signed observation or draft remains data.
    The returned JSON snapshot cannot be changed through caller aliases.
    """
    if type(tool) is not str or not tool.startswith("secretary_") or tool not in _catalog(allowed_tools):
        raise AuthorityDenied("role", "tool is outside the worker role")
    if type(args) is not dict:
        raise ValueError("worker tool arguments must be an object")
    if RESERVED_ARGUMENTS.intersection(args):
        raise AuthorityDenied("role", "worker arguments contain reserved authority fields")
    return json.loads(canonical_json(args))


@dataclass(frozen=True)
class WorkerBinding:
    token: str = field(repr=False)
    transport_id: str
    context: ExecutionContext
    expires_at: float


@dataclass(frozen=True)
class WorkerDrainResult:
    state: str
    lifecycle_id: str
    epoch: int
    disposition: str


class WorkerSessions:
    """Durable capability registry; its methods are trusted host operations."""

    def __init__(self, store, coordinator, *, drain_timeout=5):
        self.store, self.coordinator = store, coordinator
        self.drain_timeout = _positive_number(drain_timeout, "drain timeout")
        with store.transaction() as uow:
            uow.connection.execute("""CREATE TABLE IF NOT EXISTS worker_sessions (
                token_hash TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES execution_contexts(run_id),
                transport_id TEXT NOT NULL,session TEXT NOT NULL,expires_at REAL NOT NULL,
                state TEXT NOT NULL CHECK(state IN('Active','Closed')))""")

    @staticmethod
    def _token_hash(token):
        if type(token) is not str or not token or len(token) > 256:
            raise AuthorityDenied("missing", "worker capability is unknown")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _row(uow, token_hash, transport_id, session, *, now=None):
        row = uow.connection.execute("SELECT * FROM worker_sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if row is None or row["transport_id"] != transport_id or row["session"] != session:
            raise AuthorityDenied("missing", "worker capability does not bind this host transport and session")
        if now is not None:
            if row["state"] != "Active":
                raise AuthorityDenied("disabled", "worker capability is closed")
            if row["expires_at"] <= now:
                raise AuthorityDenied("expired", "worker capability has expired")
        return row

    async def open(self, session, transport_id, *, ttl=3600):
        identifier(session, "session")
        identifier(transport_id, "transport_id")
        ttl = _positive_number(ttl, "worker capability TTL")
        evidence = await self.coordinator.authorize_foreground(session)
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + ttl
        if not math.isfinite(expires_at):
            raise ValueError("worker capability expiry must be finite")
        with self.store.transaction() as uow:
            # Gate sampling and the local commit are separate acceptance points;
            # the context, enabled lifecycle and capability are one atomic unit.
            context = self.coordinator.begin_foreground_in(uow, evidence)
            self.coordinator.validate_local(uow, context, "open-worker", now=now)
            uow.connection.execute("INSERT INTO worker_sessions VALUES(?,?,?,?,?,'Active')",
                                   (self._token_hash(token), context.run_id, transport_id, session, expires_at))
        return WorkerBinding(token, transport_id, context, expires_at)

    async def resolve(self, token, transport_id, session):
        token_hash = self._token_hash(token)
        with self.store._read_unit() as uow:
            row = self._row(uow, token_hash, transport_id, session, now=time.time())
            context = AuthorityRepository(uow).context(row["run_id"])
        await self.coordinator.checkpoint(context, "resolve-worker")
        now = time.time()
        with self.store.transaction() as uow:
            row = self._row(uow, token_hash, transport_id, session, now=now)
            if row["run_id"] != context.run_id:
                raise AuthorityDenied("stale", "worker capability context changed")
            self.coordinator.validate_local(uow, context, "resolve-worker", now=now)
        return context

    @staticmethod
    def _unresolved_projection(uow, lifecycle_id):
        db = uow.connection
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_outbox'").fetchone():
            return False
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_producers'").fetchone():
            if db.execute("""SELECT 1 FROM scheduler_producers p
                JOIN scheduler_outbox o USING(declaration_key)
                JOIN scheduled_occurrences c USING(occurrence_id)
                WHERE c.lifecycle_id=? AND p.state IN('Creating','Unknown') LIMIT 1""", (lifecycle_id,)).fetchone():
                return True
        return db.execute("""SELECT 1 FROM scheduler_outbox o
            JOIN scheduled_occurrences c USING(occurrence_id)
            WHERE c.lifecycle_id=? AND o.phase<>'Removed' LIMIT 1""", (lifecycle_id,)).fetchone() is not None

    @staticmethod
    def _account(uow, lifecycle_id):
        db = uow.connection
        db.execute("""UPDATE operations SET state='Unknown' WHERE state='Applying'
            AND operation_id IN(SELECT x.operation_id FROM execution_operations x
            JOIN execution_contexts c USING(run_id) WHERE c.lifecycle_id=?)""", (lifecycle_id,))
        db.execute("""UPDATE authority_retrievals SET state='Unknown'
            WHERE state='Applying' AND run_id IN
            (SELECT run_id FROM execution_contexts WHERE lifecycle_id=?)""", (lifecycle_id,))
        authority = AuthorityRepository(uow)
        contexts = tuple(authority.context(row[0]) for row in db.execute(
            "SELECT run_id FROM execution_contexts WHERE lifecycle_id=? AND state IN('Active','Draining')",
            (lifecycle_id,)))
        for context in contexts:
            unknown = db.execute("""SELECT 1 FROM operations o
                JOIN execution_operations x USING(operation_id)
                WHERE x.run_id=? AND o.state='Unknown' LIMIT 1""", (context.run_id,)).fetchone() is not None
            if isinstance(context, RunContext):
                # Retain physical-source hazards, including an unfinished
                # prepared retrieval, until exact no-effect/verification proof.
                unknown = unknown or db.execute("SELECT unresolved_retrieval_id FROM source_guard WHERE source_id=?",
                                                (context.source_id,)).fetchone()[0] is not None
                unknown = unknown or db.execute("SELECT 1 FROM authority_retrievals WHERE run_id=? AND state IN('Applying','Unknown') LIMIT 1",
                                                (context.run_id,)).fetchone() is not None
            authority.finish(context, "Unknown" if unknown else "Cancelled")

    async def close(self, token, transport_id, session):
        token_hash = self._token_hash(token)
        # Capability revocation and lifecycle fencing happen before the first
        # await. Expired capabilities can still clean up their own lifetime.
        with self.store.transaction() as uow:
            row = self._row(uow, token_hash, transport_id, session)
            context = AuthorityRepository(uow).context(row["run_id"])
            lifecycle = uow.connection.execute("SELECT * FROM execution_lifecycle WHERE lifecycle_id=?",
                                               (context.lifecycle_id,)).fetchone()
            uow.connection.execute("UPDATE worker_sessions SET state='Closed' WHERE token_hash=?", (token_hash,))
            if lifecycle["status"] == "Stopped":
                return WorkerDrainResult("Stopped", context.lifecycle_id, lifecycle["epoch"], "drained")
            epoch = LifecycleTx(uow).disable(context.lifecycle_id)
        try:
            async with DesktopOwnership(self.store.path, timeout=self.drain_timeout) as owner:
                owner.validate()
                with self.store.transaction() as uow:
                    lifecycle = uow.connection.execute("SELECT * FROM execution_lifecycle WHERE lifecycle_id=?",
                                                       (context.lifecycle_id,)).fetchone()
                    if lifecycle["status"] == "Stopped" and lifecycle["epoch"] == epoch:
                        return WorkerDrainResult("Stopped", context.lifecycle_id, epoch, "drained")
                    if lifecycle["status"] != "Draining" or lifecycle["epoch"] != epoch:
                        raise AuthorityDenied("stale", "worker lifecycle changed while draining")
                    self._account(uow, context.lifecycle_id)
                    if self._unresolved_projection(uow, context.lifecycle_id):
                        return WorkerDrainResult("Draining", context.lifecycle_id, epoch, "unresolved_projection")
                    # This evidence is only recorded while the real, retained
                    # OS lock proves previous desktop actors have quiesced.
                    owner.validate()
                    lifecycle_tx = LifecycleTx(uow)
                    lifecycle_tx.record_drain(context.lifecycle_id, epoch)
                    lifecycle_tx.mark_stopped(context.lifecycle_id, epoch)
                return WorkerDrainResult("Stopped", context.lifecycle_id, epoch, "drained")
        except TimeoutError:
            return WorkerDrainResult("Draining", context.lifecycle_id, epoch, "timeout")


class WorkerSession:
    """Bound model dispatch object, constructed from a host-resolved binding."""

    def __init__(self, secretary, binding, *, allowed_tools=None):
        if not isinstance(binding, WorkerBinding) or not isinstance(binding.context, ExecutionContext):
            raise TypeError("worker dispatch requires a host-resolved WorkerBinding")
        self._secretary = secretary
        self._context, self._expires_at = binding.context, binding.expires_at
        self._allowed_tools = _catalog(allowed_tools)

    async def call(self, tool, args):
        snapshot = validate_worker_call(tool, args, allowed_tools=self._allowed_tools)
        if self._expires_at <= time.time():
            raise AuthorityDenied("expired", "worker capability has expired")
        return await self._secretary.call_bound(tool, snapshot, self._context)
