"""Human control operations; absent from model tool catalogs and arguments."""

import time
import uuid

from .authority_contracts import JobGrant
from .coordinator import AuthorityRepository, LifecycleTx


class Operator:
    def __init__(self, secretary, governance):
        self.secretary, self.governance = secretary, governance
        self.store, self.coordinator = secretary.ledger.store, secretary.coordinator

    def configure(self, source):
        with self.store.transaction() as unit:
            return AuthorityRepository(unit).configure_source(source)

    async def grant(self, source_id, session, expires_at, *, text=None):
        now = time.time()
        if type(expires_at) not in (int, float) or not now < expires_at <= now + 366 * 86400:
            raise ValueError("grant expiry must be within the next 366 days")
        if text is not None:
            session = "standing:" + source_id + ":" + str(uuid.uuid4())
            self.governance.intake(session, str(uuid.uuid4()), text, "external_user", new_task=True)
        evidence = await self.coordinator.authorize_foreground(session)
        with self.store.transaction() as unit:
            authority = AuthorityRepository(unit)
            authority._admission(evidence)
            source = authority.source(source_id)
            grant = JobGrant(str(uuid.uuid4()), source_id, source.revision, session,
                             evidence.admission_id, evidence.task_id, evidence.input_event_id,
                             source.scope_digest, source.scope["recipe"], expires_at)
            return authority.create_grant(grant)

    def sources(self):
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT s.*,g.revoked,g.active_run_id,g.unresolved_retrieval_id FROM authority_sources s JOIN source_guard g USING(source_id) WHERE s.revision=g.current_revision ORDER BY source_id")]

    def revoke(self, source_id):
        with self.store.transaction() as unit:
            AuthorityRepository(unit).revoke_source(source_id)

    async def collect(self, grant_id, *, run_id=None):
        if run_id:
            with self.store._read_unit() as unit:
                context = AuthorityRepository(unit).context(run_id)
            if context.grant_id != grant_id:
                raise PermissionError("resume belongs to another collection grant")
            await self.coordinator.checkpoint(context, "operator-resume")
            with self.store.read() as db:
                row = db.execute("""SELECT r.retrieval_id,e.path FROM source_guard g
                    JOIN authority_retrievals r ON r.retrieval_id=g.unresolved_retrieval_id
                    JOIN desktop_retrieval_evidence e USING(retrieval_id)
                    WHERE g.source_id=? AND r.run_id=?""", (context.source_id, context.run_id)).fetchone() if self.secretary.collection.desktop is not None else None
            if row:
                return await self.ingest(context.run_id, row["retrieval_id"], row["path"])
        else:
            evidence = await self.coordinator.authorize_grant(grant_id)
            lifecycle = "operator-collection:" + str(uuid.uuid4())
            with self.store.transaction() as unit:
                epoch = LifecycleTx(unit).start(lifecycle)
                context = self.coordinator.claim_in(unit, grant_id, evidence, lifecycle, epoch, now=time.time())
        try:
            result = await self.secretary.collection.collect(context, await self.coordinator.source(context))
            await self.coordinator.finish_collection(context, result.status)
            if result.status == "Completed":
                await self._close_lifecycle(context.lifecycle_id)
            return result
        except BaseException:
            await self.coordinator.finish(context, "Unknown")
            raise

    async def ingest(self, run_id, retrieval_id, path):
        with self.store._read_unit() as unit:
            context = AuthorityRepository(unit).context(run_id)
        source = await self.coordinator.source(context)
        port = self.secretary.collection.desktop
        candidate = await port.correlate(context, source, retrieval_id, path)
        result = await self.secretary.collection.ingest_run(context, source, retrieval_id, candidate)
        await self.coordinator.finish_collection(context, result.status)
        if result.status == "Completed":
            await self._close_lifecycle(context.lifecycle_id)
        return result

    async def _close_lifecycle(self, lifecycle_id):
        if not lifecycle_id.startswith("operator-collection:"):
            return  # A resumed scheduled run does not own the scheduler lifetime.
        from .desktop_owner import DesktopOwnership
        from .worker_sessions import WorkerSessions
        with self.store.transaction() as unit:
            epoch = LifecycleTx(unit).disable(lifecycle_id)
        async with DesktopOwnership(self.store.path, timeout=5) as owner:
            with self.store.transaction() as unit:
                WorkerSessions._account(unit, lifecycle_id)
                owner.validate()
                lifecycle = LifecycleTx(unit)
                lifecycle.record_drain(lifecycle_id, epoch)
                lifecycle.mark_stopped(lifecycle_id, epoch)
