"""Server-derived run identities and short, composable authority predicates.

External Ghost checks are sampled outside SQLite transactions. Their response and
the local commit are separate acceptance points, never a global transaction.
Constructing a context or GateEvidence does not authorize a worker transport.
"""

from dataclasses import fields
from pathlib import PurePath
import time
import uuid

from .authority_contracts import ExecutionContext, GateEvidence, JobGrant, RunContext, SourceSpec
from .contracts import identifier


class AuthorityDenied(PermissionError):
    def __init__(self, disposition, message):
        super().__init__(message)
        self.disposition = disposition


class LifecycleTx:
    def __init__(self, uow):
        self.db = uow.connection

    def start(self, lifecycle_id):
        identifier(lifecycle_id, "lifecycle_id")
        self.db.execute("INSERT OR IGNORE INTO execution_lifecycle VALUES(?,0,0,'Stopped')", (lifecycle_id,))
        changed = self.db.execute("UPDATE execution_lifecycle SET epoch=epoch+1,enabled=1,status='Running' WHERE lifecycle_id=? AND status='Stopped'", (lifecycle_id,)).rowcount
        if changed != 1:
            raise AuthorityDenied("disabled", "lifecycle must finish draining before starting")
        return self.db.execute("SELECT epoch FROM execution_lifecycle WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()[0]

    def disable(self, lifecycle_id):
        row = self.db.execute("SELECT * FROM execution_lifecycle WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()
        if not row:
            raise AuthorityDenied("missing", "unknown lifecycle")
        if row["status"] != "Draining":
            self.db.execute("UPDATE execution_lifecycle SET epoch=epoch+1,enabled=0,status='Draining' WHERE lifecycle_id=?", (lifecycle_id,))
        self.db.execute("UPDATE execution_contexts SET state='Draining' WHERE lifecycle_id=? AND state='Active'", (lifecycle_id,))
        return self.db.execute("SELECT epoch FROM execution_lifecycle WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()[0]

    def record_drain(self, lifecycle_id, epoch):
        # The lifetime owner calls this only after owned actors and projections
        # are accounted for. Stored predicates cannot prove process quiescence.
        if self.db.execute("SELECT 1 FROM execution_contexts WHERE lifecycle_id=? AND state IN('Active','Draining')", (lifecycle_id,)).fetchone():
            raise AuthorityDenied("draining", "run actors are not accounted for")
        if self.db.execute("SELECT 1 FROM operations o JOIN execution_operations x USING(operation_id) JOIN execution_contexts c ON c.run_id=x.run_id WHERE c.lifecycle_id=? AND o.state='Applying'", (lifecycle_id,)).fetchone():
            raise AuthorityDenied("draining", "an effect is still Applying")
        self.db.execute("INSERT OR REPLACE INTO lifecycle_drains VALUES(?,?)", (lifecycle_id, epoch))

    def mark_stopped(self, lifecycle_id, epoch):
        if not self.db.execute("SELECT 1 FROM lifecycle_drains WHERE lifecycle_id=? AND epoch=?", (lifecycle_id, epoch)).fetchone():
            raise AuthorityDenied("draining", "drain completion is not recorded")
        if self.db.execute("UPDATE execution_lifecycle SET status='Stopped' WHERE lifecycle_id=? AND epoch=? AND enabled=0 AND status='Draining'", (lifecycle_id, epoch)).rowcount != 1:
            raise AuthorityDenied("stale", "lifecycle generation changed while draining")


class AuthorityRepository:
    """Local authority repository; all methods use the supplied connection."""

    def __init__(self, uow):
        self.db = uow.connection

    @staticmethod
    def initialize(uow):
        uow.connection.execute("CREATE TABLE IF NOT EXISTS lifecycle_drains(lifecycle_id TEXT PRIMARY KEY REFERENCES execution_lifecycle(lifecycle_id),epoch INTEGER NOT NULL)")
        uow.connection.execute("CREATE TABLE IF NOT EXISTS execution_admissions(session TEXT PRIMARY KEY,admission_id TEXT NOT NULL,task_id TEXT NOT NULL REFERENCES tasks(task_id),input_event_id TEXT NOT NULL)")

    def record_admission(self, session, admission_id, task_id, input_event_id):
        values = (session, admission_id, task_id, input_event_id)
        for name, value in zip(("session", "admission_id", "task_id", "input_event_id"), values):
            identifier(value, name)
        prior = self.db.execute("SELECT admission_id,task_id,input_event_id FROM execution_admissions WHERE session=?", (session,)).fetchone()
        if prior is not None and tuple(prior) == values[1:]:
            return
        self.db.execute("INSERT OR REPLACE INTO execution_admissions VALUES(?,?,?,?)", values)
        self.db.execute("UPDATE execution_contexts SET state='Draining' WHERE session=? AND state='Active'", (session,))

    def _admission(self, identity):
        if not self.db.execute("SELECT 1 FROM execution_admissions WHERE session=? AND admission_id=? AND task_id=? AND input_event_id=?", tuple(getattr(identity, name) for name in ("session", "admission_id", "task_id", "input_event_id"))).fetchone():
            raise AuthorityDenied("stale", "current local admission generation changed")

    def source(self, source_id, revision=None):
        if revision is None:
            row = self.db.execute("SELECT s.* FROM authority_sources s JOIN source_guard g ON g.source_id=s.source_id AND g.current_revision=s.revision WHERE s.source_id=?", (source_id,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM authority_sources WHERE source_id=? AND revision=?", (source_id, revision)).fetchone()
        if row is None:
            raise AuthorityDenied("missing", "source revision is unknown")
        return SourceSpec(row["source_id"], row["revision"], row["kind"], row["scope_json"])

    def configure_source(self, source):
        scope = source.scope
        root = scope.get("root")
        if not isinstance(root, str) or not PurePath(root).is_absolute() or ".." in PurePath(root).parts:
            raise ValueError("source root must be a canonical absolute path")
        if source.kind == "desktop_attachments" and not all(isinstance(scope.get(k), str) and scope[k].strip() for k in ("app", "account", "conversation")):
            raise ValueError("desktop sources require exact app/account/conversation selectors")
        existing = self.db.execute("SELECT * FROM authority_sources WHERE source_id=? AND revision=?", (source.source_id, source.revision)).fetchone()
        if existing:
            if existing["scope_json"] != source.scope_json or existing["kind"] != source.kind:
                raise ValueError("source revision is immutable")
            return source
        for row in self.db.execute("SELECT * FROM authority_sources WHERE source_id<>?", (source.source_id,)):
            other = SourceSpec(row["source_id"], row["revision"], row["kind"], row["scope_json"])
            a, b = PurePath(root), PurePath(other.scope["root"])
            roots_overlap = a == b or a in b.parents or b in a.parents
            selectors_overlap = (source.kind == other.kind == "desktop_attachments" and scope["app"] == other.scope["app"] and scope["account"] == other.scope["account"] and (scope["conversation"] == other.scope["conversation"] or "*" in (scope["conversation"], other.scope["conversation"])))
            if roots_overlap or selectors_overlap:
                raise ValueError("source aliases or overlapping scopes require one stable identity")
        guard = self.db.execute("SELECT * FROM source_guard WHERE source_id=?", (source.source_id,)).fetchone()
        if guard:
            prior = self.source(source.source_id)
            identity = ("root", "app", "account", "conversation")
            if source.kind != prior.kind or any(scope.get(key) != prior.scope.get(key) for key in identity):
                raise ValueError("a stable source identity cannot change physical scope")
            if source.revision != guard["current_revision"] + 1:
                raise ValueError("source revision must advance by one")
            if guard["active_run_id"]:
                raise AuthorityDenied("overlap", "source run must be accounted for before configuration")
            self.db.execute("UPDATE source_guard SET current_revision=?,revoked=0 WHERE source_id=?", (source.revision, source.source_id))
        else:
            if source.revision != 1:
                raise ValueError("a new source starts at revision one")
            self.db.execute("INSERT INTO source_guard(source_id,current_revision) VALUES(?,?)", (source.source_id, source.revision))
        self.db.execute("INSERT INTO authority_sources VALUES(?,?,?,?,?)", (source.source_id, source.revision, source.kind, source.scope_json, source.scope_digest))
        return source

    def grant(self, grant_id):
        row = self.db.execute("SELECT * FROM collection_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise AuthorityDenied("missing", "collection grant is unknown")
        return JobGrant(**{field.name: row[field.name] for field in fields(JobGrant)})

    def create_grant(self, grant):
        source = self.source(grant.source_id, grant.source_revision)
        if source.scope_digest != grant.scope_digest or source.scope.get("recipe") != grant.recipe:
            raise ValueError("grant must bind the complete configured source and recipe")
        old = self.db.execute("SELECT * FROM collection_grants WHERE grant_id=?", (grant.grant_id,)).fetchone()
        values = tuple(getattr(grant, f.name) for f in fields(JobGrant))
        if old:
            if tuple(old[f.name] for f in fields(JobGrant)) != values:
                raise ValueError("grant identity is immutable")
            return grant
        self.db.execute("INSERT INTO collection_grants(grant_id,source_id,source_revision,session,admission_id,task_id,input_event_id,scope_digest,recipe,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)", values)
        return grant

    def revoke_grant(self, grant_id):
        self.db.execute("UPDATE collection_grants SET revoked=1 WHERE grant_id=?", (grant_id,))
        self.db.execute("UPDATE execution_contexts SET state='Draining' WHERE run_id IN(SELECT run_id FROM authority_runs WHERE grant_id=?) AND state='Active'", (grant_id,))

    def revoke_source(self, source_id):
        self.db.execute("UPDATE source_guard SET revoked=1 WHERE source_id=?", (source_id,))
        self.db.execute("UPDATE execution_contexts SET state='Draining' WHERE run_id IN(SELECT run_id FROM authority_runs WHERE source_id=?) AND state='Active'", (source_id,))

    def _lifecycle(self, lifecycle_id, epoch):
        if not self.db.execute("SELECT 1 FROM execution_lifecycle WHERE lifecycle_id=? AND epoch=? AND enabled=1 AND status='Running'", (lifecycle_id, epoch)).fetchone():
            raise AuthorityDenied("disabled", "lifecycle generation is not enabled")

    def _grant_valid(self, grant_id, now):
        grant = self.grant(grant_id)
        row = self.db.execute("SELECT g.revoked,s.revoked AS source_revoked,s.current_revision FROM collection_grants g JOIN source_guard s USING(source_id) WHERE g.grant_id=?", (grant_id,)).fetchone()
        if row["revoked"] or row["source_revoked"]:
            raise AuthorityDenied("revoked", "collection authority was revoked")
        if grant.expires_at <= now:
            raise AuthorityDenied("expired", "collection authority has expired")
        if row["current_revision"] != grant.source_revision or self.source(grant.source_id, grant.source_revision).scope_digest != grant.scope_digest:
            raise AuthorityDenied("stale", "configured scope revision changed")
        return grant

    @staticmethod
    def _matches(grant, evidence):
        if any(getattr(grant, name) != getattr(evidence, name) for name in ("session", "admission_id", "task_id", "input_event_id")):
            raise AuthorityDenied("stale", "gate evidence belongs to another admission")

    def claim(self, grant_id, evidence, lifecycle_id, epoch, *, now, job_id=None, job_revision=None, occurrence_id=None):
        grant = self._grant_valid(grant_id, now)
        self._matches(grant, evidence)
        self._admission(grant)
        self._lifecycle(lifecycle_id, epoch)
        run_id = str(uuid.uuid4())
        changed = self.db.execute("UPDATE source_guard SET active_run_id=?,run_fence=run_fence+1 WHERE source_id=? AND current_revision=? AND revoked=0 AND active_run_id IS NULL AND unresolved_retrieval_id IS NULL", (run_id, grant.source_id, grant.source_revision)).rowcount
        if changed != 1:
            raise AuthorityDenied("overlap", "source has an active run or unresolved retrieval")
        fence = self.db.execute("SELECT run_fence FROM source_guard WHERE source_id=?", (grant.source_id,)).fetchone()[0]
        context = RunContext(run_id, grant.session, grant.admission_id, grant.task_id, grant.input_event_id, lifecycle_id, epoch, fence, grant_id=grant_id, source_id=grant.source_id, source_revision=grant.source_revision, job_id=job_id, job_revision=job_revision, occurrence_id=occurrence_id)
        self._insert_context(context)
        self.db.execute("INSERT INTO authority_runs VALUES(?,?,?,?,?,?,?,?,?,?, 'Running')", (run_id, grant_id, grant.source_id, grant.source_revision, job_id, job_revision, occurrence_id, lifecycle_id, epoch, fence))
        return context

    def _insert_context(self, context):
        self.db.execute("INSERT INTO execution_contexts VALUES(?,?,?,?,?,?,?,?,?, 'Active')", tuple(getattr(context, name) for name in ("run_id", "session", "admission_id", "task_id", "input_event_id", "lifecycle_id", "epoch", "run_fence", "desktop_fence")))

    def context(self, run_id):
        row = self.db.execute("SELECT * FROM execution_contexts WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise AuthorityDenied("missing", "execution context is unknown")
        base = {f.name: row[f.name] for f in fields(ExecutionContext)}
        run = self.db.execute("SELECT * FROM authority_runs WHERE run_id=?", (run_id,)).fetchone()
        return RunContext(**base, **{name: run[name] for name in ("grant_id", "source_id", "source_revision", "job_id", "job_revision", "occurrence_id")}) if run else ExecutionContext(**base)

    def validate(self, context, *, now):
        stored = self.context(context.run_id)
        if stored != context:
            raise AuthorityDenied("stale", "execution identity or desktop generation changed")
        self._admission(context)
        row = self.db.execute("SELECT state FROM execution_contexts WHERE run_id=?", (context.run_id,)).fetchone()
        if row[0] != "Active":
            raise AuthorityDenied("disabled", "execution context is no longer active")
        self._lifecycle(context.lifecycle_id, context.epoch)
        if isinstance(context, RunContext):
            grant = self._grant_valid(context.grant_id, now)
            self._matches(grant, context)
            guard = self.db.execute("SELECT * FROM source_guard WHERE source_id=?", (context.source_id,)).fetchone()
            run = self.db.execute("SELECT state FROM authority_runs WHERE run_id=?", (context.run_id,)).fetchone()
            if guard["active_run_id"] != context.run_id or guard["run_fence"] != context.run_fence or run[0] not in ("Running", "Pending"):
                raise AuthorityDenied("stale", "source reservation no longer belongs to this run")
            if guard["unresolved_retrieval_id"] and not self.db.execute("SELECT 1 FROM authority_retrievals WHERE retrieval_id=? AND run_id=?", (guard["unresolved_retrieval_id"], context.run_id)).fetchone():
                raise AuthorityDenied("unknown", "another retrieval has unresolved effects")

    def finish(self, context, status):
        if status not in ("Completed", "Pending", "Unknown", "Cancelled", "Failed"):
            raise ValueError("unsupported run outcome")
        current = self.context(context.run_id)
        if current != context:
            raise AuthorityDenied("stale", "cannot finish another run generation")
        state = self.db.execute("SELECT state FROM execution_contexts WHERE run_id=?", (context.run_id,)).fetchone()[0]
        if state in ("Closed", "Unknown"):
            if isinstance(context, RunContext):
                prior = self.db.execute("SELECT state FROM authority_runs WHERE run_id=?", (context.run_id,)).fetchone()[0]
                if prior != status:
                    raise AuthorityDenied("stale", "terminal run outcome is immutable")
            return
        if isinstance(context, RunContext):
            hazard = self.db.execute("SELECT unresolved_retrieval_id FROM source_guard WHERE source_id=?", (context.source_id,)).fetchone()[0]
            if status == "Completed" and hazard:
                raise AuthorityDenied("unknown", "unresolved retrieval prevents completion")
            self.db.execute("UPDATE authority_runs SET state=? WHERE run_id=?", (status, context.run_id))
            if status != "Pending":
                self.db.execute("UPDATE source_guard SET active_run_id=NULL WHERE source_id=? AND active_run_id=? AND run_fence=?", (context.source_id, context.run_id, context.run_fence))
        target = ("Draining" if state == "Draining" else "Active") if status == "Pending" else "Unknown" if status == "Unknown" else "Closed"
        self.db.execute("UPDATE execution_contexts SET state=? WHERE run_id=?", (target, context.run_id))


class RunCoordinator:
    def __init__(self, store, gate):
        self.store, self.gate = store, gate
        with store.transaction() as uow:
            AuthorityRepository.initialize(uow)

    async def _gate(self, session, expected=None):
        binding = await self.gate(session)
        if binding.get("decision") != "allow":
            raise AuthorityDenied("blocked", "Ghost-ALICE current gate blocks execution")
        evidence = GateEvidence(session, binding["admission_id"], binding["task_id"], binding["inputEventId"], time.monotonic())
        if expected:
            AuthorityRepository._matches(expected, evidence)
        return evidence

    async def begin_foreground(self, session):
        evidence = await self.authorize_foreground(session)
        with self.store.transaction() as uow:
            return self.begin_foreground_in(uow, evidence)

    async def authorize_foreground(self, session):
        return await self._gate(session)

    def begin_foreground_in(self, uow, evidence):
        """Compose context and host capability registration in the same unit."""
        lifecycle_id = "foreground:" + str(uuid.uuid4())
        AuthorityRepository(uow)._admission(evidence)
        epoch = LifecycleTx(uow).start(lifecycle_id)
        context = ExecutionContext(str(uuid.uuid4()), evidence.session, evidence.admission_id, evidence.task_id, evidence.input_event_id, lifecycle_id, epoch, 1)
        AuthorityRepository(uow)._insert_context(context)
        return context

    async def authorize_grant(self, grant_id):
        with self.store._read_unit() as uow:
            grant = AuthorityRepository(uow).grant(grant_id)
        return await self._gate(grant.session, grant)

    def claim_in(self, uow, grant_id, evidence, lifecycle_id, epoch, *, now, **job_identity):
        return AuthorityRepository(uow).claim(grant_id, evidence, lifecycle_id, epoch, now=now, **job_identity)

    async def begin_granted(self, grant_id, lifecycle_id, epoch):
        evidence = await self.authorize_grant(grant_id)
        now = time.time()
        with self.store.transaction() as uow:
            return self.claim_in(uow, grant_id, evidence, lifecycle_id, epoch, now=now)

    async def begin_interactive(self, bound_run, source_id):
        await self.checkpoint(bound_run, "collect")
        with self.store._read_unit() as uow:
            row = uow.connection.execute("SELECT grant_id FROM collection_grants WHERE source_id=? AND session=? AND admission_id=? AND task_id=? AND input_event_id=? AND revoked=0 ORDER BY expires_at DESC LIMIT 1", (source_id, bound_run.session, bound_run.admission_id, bound_run.task_id, bound_run.input_event_id)).fetchone()
        if not row:
            raise AuthorityDenied("missing", "bound task has no collection grant for this source")
        return await self.begin_granted(row[0], bound_run.lifecycle_id, bound_run.epoch)

    def validate_local(self, uow, context, phase, *, now):
        AuthorityRepository(uow).validate(context, now=now)

    async def checkpoint(self, context, phase):
        evidence = await self._gate(context.session, context)
        now = time.time()
        with self.store.transaction() as uow:
            self.validate_local(uow, context, phase, now=now)
        return evidence

    async def source(self, context):
        await self.checkpoint(context, "read-source")
        with self.store._read_unit() as uow:
            return AuthorityRepository(uow).source(context.source_id, context.source_revision)

    async def finish(self, context, status):
        # Accounting after revocation is necessary; it is not a new authorized
        # effect or artifact publication. No external gate is required to stop.
        with self.store.transaction() as uow:
            AuthorityRepository(uow).finish(context, status)

    async def finish_collection(self, context, status):
        """Keep a live uncertain download available for read-only correlation.

        This is a normal-result path. Crash/cancellation/revocation accounting
        uses finish directly and never reactivates terminal authority.
        """
        if status != "Unknown":
            await self.finish(context, status)
            return status
        await self.checkpoint(context, "await-collection-evidence")
        with self.store.transaction() as unit:
            self.validate_local(unit, context, "await-collection-evidence", now=time.time())
            row = unit.connection.execute("""SELECT r.retrieval_id FROM source_guard g
                JOIN authority_retrievals r ON r.retrieval_id=g.unresolved_retrieval_id
                WHERE g.source_id=? AND g.active_run_id=? AND r.run_id=?
                AND r.state IN('Prepared','Applying','Unknown')""", (context.source_id, context.run_id, context.run_id)).fetchone()
            resolved = "Pending" if row else "Unknown"
            AuthorityRepository(unit).finish(context, resolved)
        return resolved
