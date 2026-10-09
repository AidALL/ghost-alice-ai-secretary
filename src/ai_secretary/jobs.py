"""Durable local job declarations and their replaceable timer projection.

Repositories use their caller's UnitOfWork. External gates and scheduler IO are
owned by JobManager/OutboxReconciler and never span a database transaction.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import time
import uuid

from .contracts import canonical_json, identifier
from .coordinator import AuthorityDenied, AuthorityRepository


def _number(value, label, minimum=0):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite number >= {minimum}")
    return float(value)


def _instant(value, label):
    if type(value) is str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"{label} requires an explicit timezone")
        value = parsed.timestamp()
    value = _number(value, label)
    if value > 253402300799:
        raise ValueError(f"{label} must be representable as an ISO timestamp")
    return value


@dataclass(frozen=True)
class ScheduleSpec:
    kind: str
    value: float | str
    start_at: float | str | None = None
    misfire: str = "skip"

    def __post_init__(self):
        if self.kind not in ("at", "every") or self.misfire not in ("skip", "coalesce"):
            raise ValueError("only at/every schedules and skip/coalesce misfires are supported")
        value = _instant(self.value, "at") if self.kind == "at" else _number(self.value, "every", 0.001)
        object.__setattr__(self, "value", value)
        if self.kind == "at" and self.start_at is not None:
            raise ValueError("at schedules cannot have start_at")
        if self.start_at is not None:
            object.__setattr__(self, "start_at", _instant(self.start_at, "start_at"))

    @property
    def canonical(self):
        return canonical_json({"kind": self.kind, "value": self.value,
                               "start_at": self.start_at, "misfire": self.misfire})


@dataclass(frozen=True)
class RunnerConfig:
    python_executable: str
    state_dir: str
    timeout_seconds: int = 120

    def __post_init__(self):
        for name in ("python_executable", "state_dir"):
            value = getattr(self, name)
            if type(value) is not str or not Path(value).is_absolute() or ".." in Path(value).parts or "\x00" in value:
                raise ValueError(f"{name} requires a canonical absolute path")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("runner timeout must be bounded between 1 and 3600 seconds")

    def command(self, occurrence_id):
        identifier(occurrence_id, "occurrence_id")
        return (self.python_executable, "-m", "ai_secretary.schedule_runner", "--state",
                self.state_dir, "--occurrence", occurrence_id)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    revision: int
    grant_id: str
    source_id: str
    source_revision: int
    scope_digest: str
    schedule_json: str
    max_runs: int
    max_lateness: float
    expires_at: float
    lifecycle_id: str
    epoch: int
    enabled: bool

    @property
    def schedule(self):
        return ScheduleSpec(**json.loads(self.schedule_json))


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    job_id: str
    job_revision: int
    lifecycle_id: str
    epoch: int
    due_at: float
    state: str
    disposition: str
    run_id: str | None


@dataclass(frozen=True)
class SchedulerProjection:
    declaration_key: str
    occurrence_id: str
    job_id: str
    job_revision: int
    lifecycle_id: str
    epoch: int
    due_at: float


@dataclass(frozen=True)
class SchedulerReceipt:
    upstream_id: str
    declaration_key: str
    occurrence_id: str
    due_at: float
    command_argv: tuple[str, ...]
    enabled: bool
    delivery_none: bool
    failure_alert_disabled: bool

    def matches(self, projection, config):
        return (self.declaration_key == projection.declaration_key
                and self.occurrence_id == projection.occurrence_id
                and abs(self.due_at - projection.due_at) < .001
                and self.command_argv == config.command(projection.occurrence_id))


@dataclass(frozen=True)
class ClaimResult:
    disposition: str
    state: str
    occurrence_id: str
    context: object = None


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS scheduled_jobs (
        job_id TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(revision>0),
        grant_id TEXT NOT NULL REFERENCES collection_grants(grant_id),
        source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,scope_digest TEXT NOT NULL,
        schedule_json TEXT NOT NULL,max_runs INTEGER NOT NULL CHECK(max_runs>0),
        max_lateness REAL NOT NULL CHECK(max_lateness>=0),expires_at REAL NOT NULL,
        lifecycle_id TEXT NOT NULL REFERENCES execution_lifecycle(lifecycle_id),
        epoch INTEGER NOT NULL CHECK(epoch>0),enabled INTEGER NOT NULL CHECK(enabled IN(0,1)))""",
    """CREATE TABLE IF NOT EXISTS scheduled_occurrences (
        occurrence_id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES scheduled_jobs(job_id),
        job_revision INTEGER NOT NULL,lifecycle_id TEXT NOT NULL,epoch INTEGER NOT NULL,
        due_at REAL NOT NULL,state TEXT NOT NULL CHECK(state IN
        ('Projecting','Scheduled','Running','Completed','Pending','Unknown','Cancelled','Failed','Skipped','Rejected')),
        disposition TEXT NOT NULL,run_id TEXT UNIQUE REFERENCES authority_runs(run_id),
        UNIQUE(job_id,job_revision,due_at))""",
    """CREATE TABLE IF NOT EXISTS scheduler_outbox (
        declaration_key TEXT PRIMARY KEY,occurrence_id TEXT NOT NULL UNIQUE REFERENCES scheduled_occurrences(occurrence_id),
        phase TEXT NOT NULL CHECK(phase IN('Pending','Creating','Unknown','Acked','Removing','Removed')),
        upstream_id TEXT UNIQUE,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS scheduler_producers (
        producer_id TEXT PRIMARY KEY,declaration_key TEXT NOT NULL REFERENCES scheduler_outbox(declaration_key),
        state TEXT NOT NULL CHECK(state IN('Creating','Unknown','Accounted')),upstream_id TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS scheduler_one_uncertain_producer
        ON scheduler_producers(declaration_key) WHERE state IN('Creating','Unknown')""",
)


class JobRepository:
    def __init__(self, uow):
        self.uow, self.db = uow, uow.connection

    @staticmethod
    def initialize(uow):
        for statement in _SCHEMA:
            uow.connection.execute(statement)
        # Upgrade older outboxes conservatively. A process lock cannot prove
        # that a pre-crash CLI or Gateway request stopped before committing.
        uow.connection.execute("""INSERT INTO scheduler_producers
            SELECT CASE WHEN o.attempts>1 THEN 'legacy-unaccounted:' ELSE 'legacy:' END||o.declaration_key,o.declaration_key,
                   CASE WHEN o.attempts>1 OR o.phase IN('Creating','Unknown') OR o.upstream_id IS NULL
                        THEN 'Unknown' ELSE 'Accounted' END,o.upstream_id
            FROM scheduler_outbox o WHERE o.attempts>0
              AND NOT EXISTS(SELECT 1 FROM scheduler_producers p WHERE p.declaration_key=o.declaration_key)""")
        uow.connection.execute("""UPDATE scheduler_outbox SET phase='Removing'
            WHERE phase='Removed' AND declaration_key IN
            (SELECT declaration_key FROM scheduler_producers WHERE state IN('Creating','Unknown'))""")

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM scheduled_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise AuthorityDenied("missing", "job declaration is unknown")
        return JobSpec(**{**dict(row), "enabled": bool(row["enabled"])})

    def jobs(self):
        return tuple(self.job(row[0]) for row in self.db.execute("SELECT job_id FROM scheduled_jobs ORDER BY job_id"))

    def occurrence(self, occurrence_id):
        row = self.db.execute("SELECT * FROM scheduled_occurrences WHERE occurrence_id=?", (occurrence_id,)).fetchone()
        if row is None:
            raise AuthorityDenied("missing", "occurrence is unknown")
        return Occurrence(**dict(row))

    def occurrences(self, job_id):
        return tuple(Occurrence(**dict(row)) for row in self.db.execute("SELECT * FROM scheduled_occurrences WHERE job_id=? ORDER BY due_at,occurrence_id", (job_id,)))

    def projection(self, occurrence_id):
        occurrence = self.occurrence(occurrence_id)
        row = self.db.execute("SELECT declaration_key FROM scheduler_outbox WHERE occurrence_id=?", (occurrence_id,)).fetchone()
        return SchedulerProjection(row[0], occurrence.occurrence_id, occurrence.job_id,
                                   occurrence.job_revision, occurrence.lifecycle_id, occurrence.epoch, occurrence.due_at)

    def outbox(self, *, include_removed=False):
        condition = "" if include_removed else " WHERE phase<>'Removed'"
        return tuple(dict(row) for row in self.db.execute("SELECT * FROM scheduler_outbox" + condition + " ORDER BY declaration_key"))

    def uncertain(self, declaration_key):
        return self.db.execute("SELECT 1 FROM scheduler_producers WHERE declaration_key=? AND state IN('Creating','Unknown')", (declaration_key,)).fetchone() is not None

    def start_producer(self, declaration_key):
        if self.uncertain(declaration_key):
            raise AuthorityDenied("unknown", "a previous projection producer is unaccounted")
        changed = self.db.execute("UPDATE scheduler_outbox SET phase='Creating',attempts=attempts+1 WHERE declaration_key=? AND phase='Pending'", (declaration_key,)).rowcount
        if changed != 1:
            raise AuthorityDenied("stale", "projection is not available to a new producer")
        self.db.execute("INSERT INTO scheduler_producers VALUES(?,?,'Creating',NULL)", (str(uuid.uuid4()), declaration_key))

    def remember(self, projection, receipt, config):
        if not isinstance(receipt, SchedulerReceipt) or not receipt.matches(projection, config):
            raise ValueError("listed timer does not bind the exact declaration and payload")
        # Legacy multiple attempts did not record each producer separately.
        # One listed identity cannot account for all of those possible adds.
        self.db.execute("UPDATE scheduler_producers SET state='Accounted',upstream_id=? WHERE declaration_key=? AND state IN('Creating','Unknown') AND producer_id NOT LIKE 'legacy-unaccounted:%'", (receipt.upstream_id, projection.declaration_key))
        self.db.execute("UPDATE scheduler_outbox SET upstream_id=? WHERE declaration_key=?", (receipt.upstream_id, projection.declaration_key))

    def retire(self, declaration_key, disposition):
        self.db.execute("""UPDATE scheduler_outbox SET phase=CASE
            WHEN phase='Removed' THEN 'Removed'
            WHEN phase='Pending' AND upstream_id IS NULL AND NOT EXISTS
                (SELECT 1 FROM scheduler_producers p WHERE p.declaration_key=scheduler_outbox.declaration_key AND p.state IN('Creating','Unknown')) THEN 'Removed'
            ELSE 'Removing' END,last_error=? WHERE declaration_key=?""", (disposition, declaration_key))

    def projection_failed(self, declaration_key, error):
        self.db.execute("UPDATE scheduler_producers SET state='Unknown' WHERE declaration_key=? AND state='Creating'", (declaration_key,))
        self.db.execute("UPDATE scheduler_outbox SET phase=CASE WHEN phase IN('Removing','Removed') THEN phase ELSE 'Unknown' END,last_error=? WHERE declaration_key=?", (type(error).__name__, declaration_key))

    def _valid(self, job, occurrence, *, now):
        if not job.enabled or job.revision != occurrence.job_revision:
            raise AuthorityDenied("disabled" if not job.enabled else "stale", "job revision is not enabled")
        if job.expires_at <= now:
            raise AuthorityDenied("expired", "job declaration has expired")
        authority = AuthorityRepository(self.uow)
        if (job.lifecycle_id, job.epoch) != (occurrence.lifecycle_id, occurrence.epoch):
            raise AuthorityDenied("stale", "job and occurrence scheduler generation differ")
        authority._lifecycle(occurrence.lifecycle_id, occurrence.epoch)
        grant = authority._grant_valid(job.grant_id, now)
        authority._admission(grant)
        if (grant.source_id, grant.source_revision, grant.scope_digest) != (job.source_id, job.source_revision, job.scope_digest):
            raise AuthorityDenied("stale", "job no longer binds its grant and source")
        return grant

    def create(self, grant_id, schedule, evidence, *, now, max_runs=100, max_lateness=60, expires_at=None):
        if not isinstance(schedule, ScheduleSpec):
            raise TypeError("schedule must be a ScheduleSpec")
        if type(max_runs) is not int or not 1 <= max_runs <= 100000:
            raise ValueError("max_runs must be a bounded positive integer")
        max_lateness = _number(max_lateness, "max_lateness")
        authority = AuthorityRepository(self.uow)
        grant = authority._grant_valid(grant_id, now)
        authority._matches(grant, evidence)
        authority._admission(grant)
        expiry = grant.expires_at if expires_at is None else _instant(expires_at, "expires_at")
        if expiry <= now or expiry > grant.expires_at:
            raise ValueError("job expiry must be current and within the stored grant expiry")
        lifecycle = self.db.execute("SELECT epoch FROM execution_lifecycle WHERE lifecycle_id='scheduler' AND enabled=1 AND status='Running'").fetchone()
        if lifecycle is None:
            raise AuthorityDenied("disabled", "scheduler lifecycle is not running")
        due = schedule.value if schedule.kind == "at" else schedule.start_at if schedule.start_at is not None else now + schedule.value
        if due < now or due >= expiry:
            raise ValueError("first occurrence must be current and before expiry")
        job = JobSpec(str(uuid.uuid4()), 1, grant_id, grant.source_id, grant.source_revision,
                      grant.scope_digest, schedule.canonical, max_runs, max_lateness, expiry,
                      "scheduler", lifecycle[0], True)
        self.db.execute("INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(job.__dict__.values()))
        self._allocate(job, due)
        return job

    def _allocate(self, job, due):
        occurrence_id = str(uuid.uuid4())
        self.db.execute("INSERT INTO scheduled_occurrences VALUES(?,?,?,?,?,?,'Projecting','',NULL)",
                        (occurrence_id, job.job_id, job.revision, job.lifecycle_id, job.epoch, due))
        key = f"secretary:{job.job_id}:{job.revision}:{job.epoch}:{occurrence_id}"
        self.db.execute("INSERT INTO scheduler_outbox(declaration_key,occurrence_id,phase) VALUES(?,?,'Pending')", (key, occurrence_id))
        return self.occurrence(occurrence_id)

    def reject(self, occurrence_id, disposition):
        self.db.execute("UPDATE scheduled_occurrences SET state='Rejected',disposition=? WHERE occurrence_id=? AND state IN('Projecting','Scheduled')", (disposition, occurrence_id))
        occurrence = self.occurrence(occurrence_id)
        return ClaimResult(disposition, occurrence.state, occurrence_id)

    def claim(self, occurrence_id, evidence, coordinator, *, now):
        occurrence = self.occurrence(occurrence_id)
        if occurrence.state not in ("Projecting", "Scheduled"):
            return ClaimResult(occurrence.disposition or "duplicate", occurrence.state, occurrence_id)
        job = self.job(occurrence.job_id)
        try:
            self._valid(job, occurrence, now=now)
            if now < occurrence.due_at:
                return ClaimResult("early", occurrence.state, occurrence_id)
            if now - occurrence.due_at > job.max_lateness and job.schedule.misfire == "skip":
                self.db.execute("UPDATE scheduled_occurrences SET state='Skipped',disposition='misfire' WHERE occurrence_id=?", (occurrence_id,))
                return ClaimResult("misfire", "Skipped", occurrence_id)
            count = self.db.execute("SELECT count(*) FROM scheduled_occurrences WHERE job_id=? AND run_id IS NOT NULL", (job.job_id,)).fetchone()[0]
            if count >= job.max_runs:
                raise AuthorityDenied("limit", "job run limit is exhausted")
            context = coordinator.claim_in(self.uow, job.grant_id, evidence, occurrence.lifecycle_id,
                                           occurrence.epoch, now=now, job_id=job.job_id,
                                           job_revision=occurrence.job_revision, occurrence_id=occurrence_id)
        except AuthorityDenied as error:
            return self.reject(occurrence_id, error.disposition)
        self.db.execute("UPDATE scheduled_occurrences SET state='Running',disposition='',run_id=? WHERE occurrence_id=?", (context.run_id, occurrence_id))
        return ClaimResult("claimed", "Running", occurrence_id, context)

    def finish(self, context, status):
        row = self.db.execute("SELECT state FROM authority_runs WHERE run_id=? AND occurrence_id=?", (context.run_id, context.occurrence_id)).fetchone()
        if row is None or row[0] != status:
            raise AuthorityDenied("stale", "run accounting must precede occurrence accounting")
        self.db.execute("UPDATE scheduled_occurrences SET state=?,disposition=? WHERE occurrence_id=? AND run_id=? AND state IN('Running','Pending')", (status, status.lower(), context.occurrence_id, context.run_id))

    def cancel(self, job_id):
        self.job(job_id)
        self.db.execute("UPDATE scheduled_jobs SET enabled=0,revision=revision+1 WHERE job_id=? AND enabled=1", (job_id,))
        self.db.execute("UPDATE execution_contexts SET state='Draining' WHERE state='Active' AND run_id IN(SELECT run_id FROM scheduled_occurrences WHERE job_id=?)", (job_id,))
        self.db.execute("UPDATE scheduled_occurrences SET state='Cancelled',disposition='cancelled' WHERE job_id=? AND state IN('Projecting','Scheduled')", (job_id,))
        self.db.execute("UPDATE scheduler_outbox SET phase=CASE WHEN phase='Pending' THEN 'Removed' ELSE 'Removing' END WHERE occurrence_id IN(SELECT occurrence_id FROM scheduled_occurrences WHERE job_id=?) AND phase<>'Removed'", (job_id,))

    def acknowledge(self, projection, receipt, config, *, now):
        if not isinstance(receipt, SchedulerReceipt) or not receipt.matches(projection, config):
            raise ValueError("scheduler acknowledgement does not match the exact occurrence command")
        if not receipt.delivery_none or not receipt.failure_alert_disabled:
            raise ValueError("scheduler projection permits delivery or failure alerts")
        self.remember(projection, receipt, config)
        occurrence = self.occurrence(projection.occurrence_id)
        phase = "Acked"
        try:
            self._valid(self.job(occurrence.job_id), occurrence, now=now)
        except AuthorityDenied as error:
            phase = "Removing"
            self.reject(occurrence.occurrence_id, error.disposition)
        if occurrence.state not in ("Projecting", "Scheduled", "Running", "Pending"):
            phase = "Removing"
        self.db.execute("UPDATE scheduler_outbox SET upstream_id=?,phase=?,last_error='' WHERE declaration_key=?", (receipt.upstream_id, phase, projection.declaration_key))
        if phase == "Acked":
            self.db.execute("UPDATE scheduled_occurrences SET state='Scheduled' WHERE occurrence_id=? AND state='Projecting'", (occurrence.occurrence_id,))

    def refresh_accounting(self):
        self.db.execute("UPDATE scheduled_occurrences SET state=(SELECT r.state FROM authority_runs r WHERE r.run_id=scheduled_occurrences.run_id) WHERE state IN('Running','Pending') AND run_id IN(SELECT run_id FROM authority_runs WHERE state IN('Completed','Unknown','Cancelled','Failed','Pending'))")

    def allocate_next(self, *, now):
        self.refresh_accounting()
        allocated = []
        for job in self.jobs():
            if not job.enabled or job.schedule.kind != "every":
                continue
            previous = self.occurrences(job.job_id)[-1]
            if previous.state not in ("Completed", "Skipped"):
                continue
            if self.db.execute("SELECT 1 FROM scheduler_outbox WHERE occurrence_id=? AND phase<>'Removed'", (previous.occurrence_id,)).fetchone():
                continue
            try:
                self._valid(job, previous, now=now)
            except AuthorityDenied:
                continue
            guard = self.db.execute("SELECT active_run_id,unresolved_retrieval_id FROM source_guard WHERE source_id=?", (job.source_id,)).fetchone()
            count = self.db.execute("SELECT count(*) FROM scheduled_occurrences WHERE job_id=? AND run_id IS NOT NULL", (job.job_id,)).fetchone()[0]
            if any(guard) or count >= job.max_runs:
                continue
            due = previous.due_at + job.schedule.value
            if due <= now:
                # Both policies allocate at most one timer: skip missed slots;
                # coalesce executes the last missed slot immediately.
                steps = math.floor((now - due) / job.schedule.value)
                due += (steps + (1 if job.schedule.misfire == "skip" else 0)) * job.schedule.value
            if due < job.expires_at:
                allocated.append(self._allocate(job, due))
        return tuple(allocated)


class JobManager:
    def __init__(self, store, coordinator, scheduler, runner_config):
        if not isinstance(runner_config, RunnerConfig):
            raise TypeError("runner_config must be a RunnerConfig")
        self.store, self.coordinator = store, coordinator
        self.scheduler, self.runner_config = scheduler, runner_config
        self._projection_owner = None
        with store.transaction() as uow:
            JobRepository.initialize(uow)

    @asynccontextmanager
    async def projection_lock(self):
        # The lock serializes current local producers across processes. It is
        # deliberately not evidence that an old CLI/Gateway request quiesced:
        # scheduler_producers retains that uncertainty after lock-owner death.
        actor = asyncio.current_task()
        if self._projection_owner is actor:
            yield
            return
        lock_path = self.store.path.parent / "scheduler-projection.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.01)
            self._projection_owner = actor
            try:
                yield
            finally:
                self._projection_owner = None
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    async def create(self, grant_id, schedule, *, now=None, **limits):
        evidence = await self.coordinator.authorize_grant(grant_id)
        now = time.time() if now is None else _number(now, "now")
        with self.store.transaction() as uow:
            return JobRepository(uow).create(grant_id, schedule, evidence, now=now, **limits)

    async def claim(self, occurrence_id, *, now=None):
        with self.store._read_unit() as uow:
            repo = JobRepository(uow)
            occurrence = repo.occurrence(occurrence_id)
            if occurrence.state not in ("Projecting", "Scheduled"):
                return ClaimResult(occurrence.disposition or "duplicate", occurrence.state, occurrence_id)
            grant_id = repo.job(occurrence.job_id).grant_id
        try:
            evidence = await self.coordinator.authorize_grant(grant_id)
        except AuthorityDenied as error:
            with self.store.transaction() as uow:
                return JobRepository(uow).reject(occurrence_id, error.disposition)
        now = time.time() if now is None else _number(now, "now")
        with self.store.transaction() as uow:
            return JobRepository(uow).claim(occurrence_id, evidence, self.coordinator, now=now)

    async def finish(self, context, status, *, live_result=False):
        if live_result:
            status = await self.coordinator.finish_collection(context, status)
        else:
            await self.coordinator.finish(context, status)
        with self.store.transaction() as uow:
            JobRepository(uow).finish(context, status)

    async def cancel(self, job_id):
        with self.store.transaction() as uow:
            JobRepository(uow).cancel(job_id)
        await OutboxReconciler(self)._cleanup()


class OutboxReconciler:
    """Call at restart and periodically, independently of collection completion."""

    def __init__(self, manager):
        self.manager = manager

    async def _cleanup(self, listed=None, *, now=None):
        async with self.manager.projection_lock():
            return await self._cleanup_locked(listed, now=now)

    async def _cleanup_locked(self, listed=None, *, now=None, fresh_inventory=False):
        manager = self.manager
        now = time.time() if now is None else now
        with manager.store.transaction() as uow:
            repo = JobRepository(uow)
            repo.refresh_accounting()
            for row in repo.outbox(include_removed=True):
                occurrence = repo.occurrence(row["occurrence_id"])
                if row["phase"] == "Acked":
                    try:
                        repo._valid(repo.job(occurrence.job_id), occurrence, now=now)
                    except AuthorityDenied as error:
                        repo.reject(occurrence.occurrence_id, error.disposition)
                        repo.retire(row["declaration_key"], error.disposition)
                    if occurrence.state not in ("Projecting", "Scheduled", "Running", "Pending"):
                        repo.retire(row["declaration_key"], "terminal")
                if listed is None:
                    continue
                matches = [receipt for receipt in listed if receipt.declaration_key == row["declaration_key"]]
                if len(matches) > 1:
                    raise ValueError("duplicate upstream declaration identities require accounting")
                current = self._outbox_row(repo, row["declaration_key"])
                if matches and current["phase"] in ("Removing", "Removed"):
                    repo.remember(repo.projection(occurrence.occurrence_id), matches[0], manager.runner_config)
                    uow.connection.execute("UPDATE scheduler_outbox SET phase='Removing' WHERE declaration_key=?", (row["declaration_key"],))
                elif (fresh_inventory and not matches and current["phase"] == "Removing"
                      and not repo.uncertain(row["declaration_key"])):
                    # Missing-ID rm is an error in the pinned Gateway. A
                    # complete listing taken after prior producer accounting
                    # confirms absence when local cleanup acknowledgement was
                    # interrupted. It never accounts for an uncertain add.
                    uow.connection.execute("UPDATE scheduler_outbox SET phase='Removed',last_error='' WHERE declaration_key=?", (row["declaration_key"],))
            rows = [row for row in repo.outbox() if row["phase"] == "Removing"]
        removed = set()
        for row in rows:
            upstream = row["upstream_id"]
            if upstream is None:
                with manager.store.transaction() as uow:
                    repo = JobRepository(uow)
                    if repo.uncertain(row["declaration_key"]):
                        uow.connection.execute("UPDATE scheduler_outbox SET last_error='unresolved-create' WHERE declaration_key=?", (row["declaration_key"],))
                    else:
                        uow.connection.execute("UPDATE scheduler_outbox SET phase='Removed',last_error='' WHERE declaration_key=?", (row["declaration_key"],))
                continue
            try:
                await manager.scheduler.remove(upstream)
            except (Exception, asyncio.CancelledError) as error:
                with manager.store.transaction() as uow:
                    uow.connection.execute("UPDATE scheduler_outbox SET upstream_id=?,phase='Removing',last_error=? WHERE declaration_key=?", (upstream, type(error).__name__, row["declaration_key"]))
                if isinstance(error, asyncio.CancelledError):
                    raise
            else:
                removed.add(upstream)
                with manager.store.transaction() as uow:
                    repo = JobRepository(uow)
                    uncertain = repo.uncertain(row["declaration_key"])
                    phase = "Removing" if uncertain else "Removed"
                    # This exact identity is now absent. Keep the declaration
                    # tombstone for other unaccounted producers, without
                    # repeatedly removing the identity just confirmed absent.
                    uow.connection.execute("UPDATE scheduler_outbox SET upstream_id=?,phase=?,last_error='' WHERE declaration_key=?", (None if uncertain else upstream, phase, row["declaration_key"]))
        return removed

    @staticmethod
    def _outbox_row(repo, key):
        return dict(repo.db.execute("SELECT * FROM scheduler_outbox WHERE declaration_key=?", (key,)).fetchone())

    def _reconcile_listing(self, repo, listed, *, now):
        manager = self.manager
        for row in repo.outbox():
            key = row["declaration_key"]
            matches = [receipt for receipt in listed if receipt.declaration_key == key]
            if len(matches) > 1:
                raise ValueError("duplicate upstream declaration identities require accounting")
            occurrence = repo.occurrence(row["occurrence_id"])
            projection = repo.projection(occurrence.occurrence_id)
            if matches:
                repo.remember(projection, matches[0], manager.runner_config)
                if row["phase"] in ("Creating", "Unknown"):
                    repo.db.execute("UPDATE scheduler_outbox SET phase='Unknown' WHERE declaration_key=?", (key,))
                elif row["phase"] == "Acked" and not (matches[0].enabled and matches[0].delivery_none and matches[0].failure_alert_disabled):
                    repo.db.execute("UPDATE scheduler_outbox SET phase='Unknown' WHERE declaration_key=?", (key,))
                continue
            if row["phase"] == "Creating":
                repo.projection_failed(key, RuntimeError("producer owner ended before accounting"))
                continue
            if repo.uncertain(key):
                # Neither an empty listing nor the released OS lock accounts
                # for an old CLI or delayed Gateway add. Never retry its add.
                continue
            if row["phase"] not in ("Acked", "Unknown"):
                continue
            job = repo.job(occurrence.job_id)
            try:
                repo._valid(job, occurrence, now=now)
            except AuthorityDenied as error:
                repo.reject(occurrence.occurrence_id, error.disposition)
                repo.db.execute("UPDATE scheduler_outbox SET phase='Removed',last_error=? WHERE declaration_key=?", (error.disposition, key))
                continue
            if occurrence.state not in ("Projecting", "Scheduled"):
                repo.db.execute("UPDATE scheduler_outbox SET phase='Removed',last_error='' WHERE declaration_key=?", (key,))
            elif now - occurrence.due_at > job.max_lateness and job.schedule.misfire == "skip":
                repo.db.execute("UPDATE scheduled_occurrences SET state='Skipped',disposition='misfire' WHERE occurrence_id=?", (occurrence.occurrence_id,))
                repo.db.execute("UPDATE scheduler_outbox SET phase='Removed',last_error='' WHERE declaration_key=?", (key,))
            else:
                # The prior add is accounted and its identified timer is
                # absent. Reproject this same unclaimed occurrence, never a run.
                repo.db.execute("UPDATE scheduler_outbox SET phase='Pending',upstream_id=NULL,last_error='timer-missing' WHERE declaration_key=?", (key,))
                repo.db.execute("UPDATE scheduled_occurrences SET state='Projecting' WHERE occurrence_id=? AND state='Scheduled'", (occurrence.occurrence_id,))

    async def tick(self, *, now=None):
        async with self.manager.projection_lock():
            await self._tick_locked(now=now)

    async def _tick_locked(self, *, now=None):
        manager = self.manager
        explicit_now = None if now is None else _number(now, "now")
        def clock():
            return time.time() if explicit_now is None else explicit_now
        # A failed list is an unknown remote state. Do not create based on it.
        listed = await manager.scheduler.list()
        if any(not isinstance(receipt, SchedulerReceipt) for receipt in listed):
            raise TypeError("scheduler list must return SchedulerReceipt values")
        removed = await self._cleanup_locked(listed, now=clock(), fresh_inventory=True)
        listed = tuple(receipt for receipt in listed if receipt.upstream_id not in removed)
        allocation_now = clock()
        with manager.store.transaction() as uow:
            repo = JobRepository(uow)
            self._reconcile_listing(repo, listed, now=allocation_now)
            repo.allocate_next(now=allocation_now)
            rows = [row for row in repo.outbox() if row["phase"] in ("Pending", "Creating", "Unknown")]
        for row in rows:
            with manager.store._read_unit() as uow:
                repo = JobRepository(uow)
                projection = repo.projection(row["occurrence_id"])
                grant_id = repo.job(projection.job_id).grant_id
            matches = [receipt for receipt in listed if receipt.declaration_key == projection.declaration_key]
            if len(matches) > 1:
                raise ValueError("duplicate upstream declaration identities")
            receipt = matches[0] if matches else None
            try:
                evidence = await manager.coordinator.authorize_grant(grant_id)
                projection_now = clock()
                with manager.store.transaction() as uow:
                    repo = JobRepository(uow)
                    current = repo.occurrence(projection.occurrence_id)
                    grant = repo._valid(repo.job(projection.job_id), current, now=projection_now)
                    AuthorityRepository._matches(grant, evidence)
                    if current.state not in ("Projecting", "Scheduled", "Running", "Pending"):
                        raise AuthorityDenied("disabled", "occurrence has already finished")
                    if receipt is None:
                        if repo.uncertain(projection.declaration_key):
                            continue
                        if current.state not in ("Projecting", "Scheduled"):
                            repo.retire(projection.declaration_key, "already-claimed")
                            continue
                        repo.start_producer(projection.declaration_key)
                    else:
                        repo.remember(projection, receipt, manager.runner_config)
                if receipt is None:
                    receipt = await manager.scheduler.create(projection, manager.runner_config)
                elif not (receipt.enabled and receipt.delivery_none and receipt.failure_alert_disabled):
                    # Add/edit interruption is recovered by editing its exact
                    # observed ID. No new creation producer is admitted here.
                    receipt = await manager.scheduler.finalize(projection, receipt, manager.runner_config)
                acknowledgement_now = clock()
                with manager.store.transaction() as uow:
                    JobRepository(uow).acknowledge(projection, receipt, manager.runner_config, now=acknowledgement_now)
            except AuthorityDenied as error:
                with manager.store.transaction() as uow:
                    repo = JobRepository(uow)
                    repo.reject(projection.occurrence_id, error.disposition)
                    repo.retire(projection.declaration_key, error.disposition)
            except (Exception, asyncio.CancelledError) as error:
                with manager.store.transaction() as uow:
                    JobRepository(uow).projection_failed(projection.declaration_key, error)
                if isinstance(error, asyncio.CancelledError):
                    raise
        # Reuse the listing only for known remote identity discovery. Current
        # state still decides cleanup, so late create acknowledgements account.
        await self._cleanup_locked(listed, now=clock())

    async def run(self, stop_event, *, interval=2):
        interval = _number(interval, "reconcile interval", .01)
        while not stop_event.is_set():
            try:
                await self.tick()
            except (OSError, RuntimeError, ValueError) as error:
                self.last_error = type(error).__name__
            else:
                self.last_error = None
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass
