import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import time
import unittest

from ai_secretary.authority import AuthorityStore
from ai_secretary.authority_contracts import JobGrant, SourceSpec
from ai_secretary.coordinator import AuthorityRepository, LifecycleTx, RunCoordinator
from ai_secretary.ledger import LedgerTx


class JobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            from ai_secretary.jobs import JobManager, JobRepository, OutboxReconciler, RunnerConfig, ScheduleSpec, SchedulerReceipt
        except ImportError as error:
            self.fail(f"durable scheduler is missing: {error}")
        self.Repository, self.Schedule = JobRepository, ScheduleSpec
        self.Reconciler, self.Receipt = OutboxReconciler, SchedulerReceipt
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.store = AuthorityStore(self.root / "operations.sqlite")
        self.binding = {"decision": "allow", "task_id": "task", "inputEventId": "event", "admission_id": "admission"}
        self.gates = []

        async def gate(session):
            self.gates.append(session)
            with self.store.transaction() as uow:
                uow.connection.execute("SELECT 1")
            return dict(self.binding)

        self.coordinator = RunCoordinator(self.store, gate)
        with self.store.transaction() as uow:
            LedgerTx(uow).create_task("task", "user", "bounded collection")
            self.source = SourceSpec.create("source", 1, "local_files", {"root": str(self.root / "incoming"), "recipe": "local-snapshot"})
            repo = AuthorityRepository(uow)
            repo.record_admission("original-session", "admission", "task", "event")
            repo.configure_source(self.source)
            repo.create_grant(JobGrant("grant", "source", 1, "original-session", "admission", "task", "event", self.source.scope_digest, "local-snapshot", self.now + 1000))
            LifecycleTx(uow).start("scheduler")
        outer = self

        class TimerPort:
            def __init__(self):
                self.receipts, self.created, self.removed = {}, [], []
                self.finalized = []
                self.before_return = None
                self.unknown_once = False

            async def list(self):
                return tuple(self.receipts.values())

            async def create(self, projection, config):
                receipt = outer.Receipt("upstream:" + projection.occurrence_id, projection.declaration_key,
                                        projection.occurrence_id, projection.due_at,
                                        config.command(projection.occurrence_id), True, True, True)
                self.receipts[projection.declaration_key] = receipt
                self.created.append(projection)
                if self.before_return:
                    await self.before_return(projection)
                if self.unknown_once:
                    self.unknown_once = False
                    raise TimeoutError("create acknowledgement lost")
                return receipt

            async def remove(self, upstream_id):
                self.removed.append(upstream_id)
                self.receipts = {key: value for key, value in self.receipts.items() if value.upstream_id != upstream_id}

            async def finalize(self, projection, receipt, config):
                from dataclasses import replace
                enabled = replace(receipt, enabled=True, delivery_none=True, failure_alert_disabled=True)
                self.receipts[projection.declaration_key] = enabled
                self.finalized.append(receipt.upstream_id)
                return enabled

        self.scheduler = TimerPort()
        self.manager = JobManager(self.store, self.coordinator, self.scheduler,
                                  RunnerConfig(str(Path(__import__('sys').executable).resolve()), str(self.root), 30))

    async def create(self, **options):
        schedule = options.pop("schedule", self.Schedule("at", self.now + 1))
        return await self.manager.create("grant", schedule, now=self.now, **options)

    def occurrence(self, job_id):
        with self.store._read_unit() as uow:
            return self.Repository(uow).occurrences(job_id)[-1]

    async def claim(self, occurrence_id, now=None):
        return await self.manager.claim(occurrence_id, now=self.now + 1 if now is None else now)

    async def test_create_derives_immutable_identity_and_preallocates_outbox(self):
        job = await self.create()
        self.assertEqual((job.grant_id, job.source_id, job.source_revision, job.scope_digest),
                         ("grant", "source", 1, self.source.scope_digest))
        with self.assertRaises(FrozenInstanceError):
            job.source_id = "forged"
        occurrence = self.occurrence(job.job_id)
        self.assertEqual(occurrence.state, "Projecting")
        self.assertEqual(len(self.scheduler.created), 0)
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.occurrence(job.job_id).state, "Scheduled")
        self.assertEqual(self.scheduler.created[0].occurrence_id, occurrence.occurrence_id)

    async def test_duplicate_callback_has_one_durable_run(self):
        job = await self.create()
        occurrence = self.occurrence(job.job_id)
        winner = await self.claim(occurrence.occurrence_id)
        loser = await self.claim(occurrence.occurrence_id)
        self.assertEqual(winner.disposition, "claimed")
        self.assertEqual((loser.disposition, loser.state), ("duplicate", "Running"))
        self.assertIsNone(loser.context)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM authority_runs").fetchone()[0], 1)

    async def test_sqlite_race_claims_one_occurrence_once(self):
        job = await self.create()
        occurrence = self.occurrence(job.job_id)
        evidence = await self.coordinator.authorize_grant("grant")

        def claim():
            with self.store.transaction() as uow:
                return self.Repository(uow).claim(occurrence.occurrence_id, evidence, self.coordinator, now=self.now + 1)

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(result.disposition for result in results), ["claimed", "duplicate"])

    async def test_pre_ack_callback_and_late_ack_cannot_reset_running(self):
        job = await self.create()

        async def callback(projection):
            self.assertEqual((await self.claim(projection.occurrence_id)).disposition, "claimed")

        self.scheduler.before_return = callback
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.occurrence(job.job_id).state, "Running")

    async def test_cancel_inflight_projection_invalidates_before_remove(self):
        job = await self.create()

        async def cancel(projection):
            await self.manager.cancel(job.job_id)
            denied = await self.claim(projection.occurrence_id)
            self.assertEqual(denied.state, "Cancelled")

        self.scheduler.before_return = cancel
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.occurrence(job.job_id).state, "Cancelled")
        self.assertEqual(len(self.scheduler.removed), 1)

    async def test_stop_during_create_accounts_late_identity_and_blocks_callback(self):
        job = await self.create()

        async def stop(projection):
            with self.store.transaction() as uow:
                LifecycleTx(uow).disable("scheduler")

        self.scheduler.before_return = stop
        await self.Reconciler(self.manager).tick(now=self.now)
        denied = await self.claim(self.occurrence(job.job_id).occurrence_id)
        self.assertEqual(denied.disposition, "disabled")
        self.assertEqual(len(self.scheduler.removed), 1)

    async def test_cancel_running_marks_execution_draining_before_external_io(self):
        job = await self.create()
        claim = await self.claim(self.occurrence(job.job_id).occurrence_id)
        await self.manager.cancel(job.job_id)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM execution_contexts WHERE run_id=?", (claim.context.run_id,)).fetchone()[0], "Draining")
            self.assertEqual(db.execute("SELECT enabled,revision FROM scheduled_jobs").fetchone()[:], (0, 2))

    async def test_pending_finish_cannot_reactivate_cancelled_execution(self):
        job = await self.create()
        claimed = await self.claim(self.occurrence(job.job_id).occurrence_id)
        await self.manager.cancel(job.job_id)
        await self.manager.finish(claimed.context, "Pending")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM execution_contexts WHERE run_id=?", (claimed.context.run_id,)).fetchone()[0], "Draining")
        with self.assertRaises(PermissionError):
            await self.coordinator.checkpoint(claimed.context, "publish")

    async def test_late_ack_after_completed_callback_never_resets_terminal(self):
        job = await self.create()
        async def callback(projection):
            claimed = await self.claim(projection.occurrence_id)
            await self.manager.finish(claimed.context, "Completed")
        self.scheduler.before_return = callback
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.occurrence(job.job_id).state, "Completed")
        self.assertEqual(len(self.scheduler.removed), 1)

    async def test_claim_rollback_keeps_occurrence_and_source_unclaimed(self):
        job = await self.create()
        occurrence = self.occurrence(job.job_id)
        evidence = await self.coordinator.authorize_grant("grant")
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction() as uow:
                self.Repository(uow).claim(occurrence.occurrence_id, evidence, self.coordinator, now=self.now + 1)
                raise RuntimeError("rollback")
        self.assertEqual(self.occurrence(job.job_id).state, "Projecting")
        self.assertEqual((await self.claim(occurrence.occurrence_id)).disposition, "claimed")

    async def test_revoked_grant_is_rejected_inside_claim_transaction(self):
        job = await self.create()
        with self.store.transaction() as uow:
            AuthorityRepository(uow).revoke_grant("grant")
        result = await self.claim(self.occurrence(job.job_id).occurrence_id)
        self.assertEqual(result.disposition, "revoked")
        self.assertEqual(result.state, "Rejected")

    async def test_expired_job_rejected_even_when_grant_live(self):
        job = await self.create(expires_at=self.now + 2)
        result = await self.claim(self.occurrence(job.job_id).occurrence_id, self.now + 3)
        self.assertEqual(result.disposition, "expired")

    async def test_source_revision_and_unresolved_hazard_block_claim(self):
        job = await self.create()
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='unknown' WHERE source_id='source'")
        result = await self.claim(self.occurrence(job.job_id).occurrence_id)
        self.assertEqual(result.disposition, "overlap")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT unresolved_retrieval_id FROM source_guard").fetchone()[0], "unknown")

    async def test_stale_revision_rejected_without_new_run(self):
        job = await self.create()
        with self.store.transaction() as uow:
            AuthorityRepository(uow).configure_source(SourceSpec.create("source", 2, "local_files", self.source.scope))
        self.assertEqual((await self.claim(self.occurrence(job.job_id).occurrence_id)).disposition, "stale")

    async def test_early_callback_does_not_consume_occurrence(self):
        job = await self.create()
        occurrence = self.occurrence(job.job_id)
        self.assertEqual((await self.claim(occurrence.occurrence_id, self.now)).disposition, "early")
        self.assertEqual((await self.claim(occurrence.occurrence_id)).disposition, "claimed")

    async def test_misfire_skip_and_coalesce_are_explicit(self):
        skip = await self.create(max_lateness=2)
        self.assertEqual((await self.claim(self.occurrence(skip.job_id).occurrence_id, self.now + 5)).state, "Skipped")
        coalesce = await self.create(schedule=self.Schedule("at", self.now + 1, misfire="coalesce"), max_lateness=2)
        self.assertEqual((await self.claim(self.occurrence(coalesce.job_id).occurrence_id, self.now + 5)).disposition, "claimed")

    async def test_unknown_create_reconciles_from_list_after_restart(self):
        job = await self.create()
        self.scheduler.unknown_once = True
        await self.Reconciler(self.manager).tick(now=self.now)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT phase FROM scheduler_outbox").fetchone()[0], "Unknown")
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.occurrence(job.job_id).state, "Scheduled")
        self.assertEqual(len(self.scheduler.created), 1)

    async def test_uncertain_create_is_not_retried_when_listing_is_empty(self):
        job = await self.create()
        remotes, creates, removed = [], [], []
        def receipt(projection, config, name):
            return self.Receipt(name, projection.declaration_key, projection.occurrence_id,
                                projection.due_at, config.command(projection.occurrence_id), True, True, True)
        async def listing():
            return tuple(remotes)
        async def creating(projection, config):
            creates.append(projection)
            if len(creates) == 1:
                raise TimeoutError("first add still pending at Gateway")
            second = receipt(projection, config, "producer-B")
            remotes.append(second)
            return second
        async def removing(upstream_id):
            removed.append(upstream_id)
            remotes[:] = [remote for remote in remotes if remote.upstream_id != upstream_id]
        self.scheduler.list, self.scheduler.create, self.scheduler.remove = listing, creating, removing
        await self.Reconciler(self.manager).tick(now=self.now)
        await self.Reconciler(self.manager).tick(now=self.now)
        await self.manager.cancel(job.job_id)
        remotes.append(receipt(creates[0], self.manager.runner_config, "producer-A"))
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(len(creates), 1)
        self.assertEqual(removed, ["producer-A"])
        self.assertEqual(remotes, [])

    async def test_two_reconcilers_share_process_projection_lock_and_one_producer(self):
        from ai_secretary.jobs import JobManager
        await self.create()
        second_manager = JobManager(self.store, self.coordinator, self.scheduler, self.manager.runner_config)
        entered, release = asyncio.Event(), asyncio.Event()
        original_create = self.scheduler.create
        calls = []
        async def delayed(projection, config):
            calls.append(projection)
            entered.set()
            await release.wait()
            return await original_create(projection, config)
        self.scheduler.create = delayed
        first = asyncio.create_task(self.Reconciler(self.manager).tick(now=self.now))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.Reconciler(second_manager).tick(now=self.now))
        await asyncio.sleep(.04)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(calls), 1)

    async def test_lost_acked_timer_reprojects_same_unclaimed_occurrence(self):
        job = await self.create()
        await self.Reconciler(self.manager).tick(now=self.now)
        original = self.occurrence(job.job_id)
        self.scheduler.receipts.clear()
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(len(self.scheduler.created), 2)
        self.assertEqual(self.occurrence(job.job_id).occurrence_id, original.occurrence_id)
        self.assertEqual((await self.claim(original.occurrence_id)).disposition, "claimed")
        self.assertEqual((await self.claim(original.occurrence_id)).disposition, "duplicate")

    async def test_lost_acked_misfire_skip_does_not_reproject_old_timer(self):
        job = await self.create(max_lateness=2)
        await self.Reconciler(self.manager).tick(now=self.now)
        self.scheduler.receipts.clear()
        await self.Reconciler(self.manager).tick(now=self.now + 5)
        self.assertEqual(len(self.scheduler.created), 1)
        self.assertEqual(self.occurrence(job.job_id).state, "Skipped")

    async def test_unknown_disabled_add_finalizes_exact_id_without_new_add(self):
        job = await self.create()
        async def partially_created(projection, config):
            remote = self.Receipt("partial-add", projection.declaration_key, projection.occurrence_id,
                                  projection.due_at, config.command(projection.occurrence_id), False, True, False)
            self.scheduler.created.append(projection)
            self.scheduler.receipts[projection.declaration_key] = remote
            raise TimeoutError("add done; edit not acknowledged")
        self.scheduler.create = partially_created
        await self.Reconciler(self.manager).tick(now=self.now)
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(len(self.scheduler.created), 1)
        self.assertEqual(self.scheduler.finalized, ["partial-add"])
        self.assertEqual(self.occurrence(job.job_id).state, "Scheduled")

    async def test_removed_tombstone_cleans_exact_late_owned_remote(self):
        job = await self.create()
        await self.manager.cancel(job.job_id)
        occurrence = self.occurrence(job.job_id)
        with self.store._read_unit() as uow:
            projection = self.Repository(uow).projection(occurrence.occurrence_id)
        remote = self.Receipt("late-owned", projection.declaration_key, occurrence.occurrence_id,
                              occurrence.due_at, self.manager.runner_config.command(occurrence.occurrence_id), True, True, True)
        self.scheduler.receipts[projection.declaration_key] = remote
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.scheduler.removed, ["late-owned"])

    async def test_restart_keeps_creating_producer_unknown_without_new_add(self):
        from ai_secretary.jobs import JobManager
        job = await self.create()
        occurrence = self.occurrence(job.job_id)
        with self.store.transaction() as uow:
            repo = self.Repository(uow)
            repo.start_producer(repo.projection(occurrence.occurrence_id).declaration_key)
        restarted = JobManager(self.store, self.coordinator, self.scheduler, self.manager.runner_config)
        await self.Reconciler(restarted).tick(now=self.now)
        self.assertEqual(self.scheduler.created, [])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT phase FROM scheduler_outbox").fetchone()[0], "Unknown")
            self.assertEqual(db.execute("SELECT state FROM scheduler_producers").fetchone()[0], "Unknown")

    async def test_projection_lock_excludes_another_operating_system_process(self):
        import sys
        program = "import fcntl, os, sys\nfd=os.open(sys.argv[1],os.O_RDWR)\ntry:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n print('acquired')\nexcept BlockingIOError:\n print('locked')\n"
        async with self.manager.projection_lock():
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", program,
                str(self.store.path.parent / "scheduler-projection.lock"), stdout=asyncio.subprocess.PIPE)
            stdout, _ = await process.communicate()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout.strip(), b"locked")

    async def test_legacy_removed_multiple_attempts_retains_uncertain_producer_hazard(self):
        from ai_secretary.jobs import JobManager
        job = await self.create()
        await self.manager.cancel(job.job_id)
        occurrence = self.occurrence(job.job_id)
        with self.store.transaction() as uow:
            projection = self.Repository(uow).projection(occurrence.occurrence_id)
            uow.connection.execute("DROP TABLE scheduler_producers")
            uow.connection.execute("UPDATE scheduler_outbox SET phase='Removed',attempts=2,upstream_id='old-B'")
        restarted = JobManager(self.store, self.coordinator, self.scheduler, self.manager.runner_config)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT phase FROM scheduler_outbox").fetchone()[0], "Removing")
            self.assertEqual(db.execute("SELECT state FROM scheduler_producers").fetchone()[0], "Unknown")
        self.scheduler.receipts[projection.declaration_key] = self.Receipt("late-A", projection.declaration_key,
            occurrence.occurrence_id, occurrence.due_at, self.manager.runner_config.command(occurrence.occurrence_id), True, True, True)
        await self.Reconciler(restarted).tick(now=self.now)
        self.assertEqual(self.scheduler.removed, ["late-A"])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT phase FROM scheduler_outbox").fetchone()[0], "Removing")
            self.assertEqual(db.execute("SELECT state FROM scheduler_producers").fetchone()[0], "Unknown")

    async def test_scheduler_list_failure_does_not_create_another_timer(self):
        job = await self.create()
        async def unavailable():
            raise TimeoutError("list unavailable")
        self.scheduler.list = unavailable
        with self.assertRaises(TimeoutError):
            await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.scheduler.created, [])
        self.assertEqual(self.occurrence(job.job_id).state, "Projecting")

    async def test_cancel_unknown_create_keeps_hazard_when_list_is_empty(self):
        job = await self.create()
        async def uncertain_create(projection, config):
            raise TimeoutError("create may still commit upstream")
        self.scheduler.create = uncertain_create
        await self.Reconciler(self.manager).tick(now=self.now)
        await self.manager.cancel(job.job_id)
        await self.Reconciler(self.manager).tick(now=self.now)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT phase FROM scheduler_outbox").fetchone()[0], "Removing")
        self.assertEqual(self.occurrence(job.job_id).state, "Cancelled")

    async def test_completed_remove_before_local_ack_reconciles_complete_absence(self):
        job = await self.create(schedule=self.Schedule("every", 10, start_at=self.now + 1), max_runs=2)
        await self.Reconciler(self.manager).tick(now=self.now)
        first = self.occurrence(job.job_id)
        claimed = await self.claim(first.occurrence_id)
        await self.manager.finish(claimed.context, "Completed")
        # The remote rm committed, then the process died before storing Removed.
        self.scheduler.receipts.clear()
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE scheduler_outbox SET phase='Removing',last_error='RuntimeError'")
        async def missing_remove(upstream_id):
            self.scheduler.removed.append(upstream_id)
            raise RuntimeError("pinned Gateway rejects missing automation")
        self.scheduler.remove = missing_remove
        await self.Reconciler(self.manager).tick(now=self.now + 2)
        with self.store.read() as db:
            phase = db.execute("SELECT phase FROM scheduler_outbox WHERE occurrence_id=?", (first.occurrence_id,)).fetchone()[0]
            self.assertEqual(phase, "Removed")
        self.assertEqual(self.scheduler.removed, [])
        self.assertNotEqual(self.occurrence(job.job_id).occurrence_id, first.occurrence_id)
        self.assertEqual(self.occurrence(job.job_id).due_at, self.now + 11)

    async def test_complete_absence_with_known_id_preserves_uncertain_producer(self):
        job = await self.create()
        first = self.occurrence(job.job_id)
        with self.store.transaction() as uow:
            repo = self.Repository(uow)
            projection = repo.projection(first.occurrence_id)
            repo.start_producer(projection.declaration_key)
            repo.projection_failed(projection.declaration_key, TimeoutError())
            uow.connection.execute("UPDATE scheduler_outbox SET upstream_id='possibly-late'")
            repo.cancel(job.job_id)
        async def missing_remove(upstream_id):
            raise RuntimeError("missing identity does not account for the old add")
        self.scheduler.remove = missing_remove
        await self.Reconciler(self.manager).tick(now=self.now)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT phase FROM scheduler_outbox").fetchone()[0], "Removing")
            self.assertEqual(db.execute("SELECT state FROM scheduler_producers").fetchone()[0], "Unknown")

    async def test_periodic_reconciler_allocates_next_without_collector_trigger(self):
        job = await self.create(schedule=self.Schedule("every", 10, start_at=self.now + 1), max_runs=2)
        await self.Reconciler(self.manager).tick(now=self.now)
        first = self.occurrence(job.job_id)
        claimed = await self.claim(first.occurrence_id)
        await self.manager.finish(claimed.context, "Completed")
        await self.Reconciler(self.manager).tick(now=self.now + 2)
        second = self.occurrence(job.job_id)
        self.assertNotEqual(first.occurrence_id, second.occurrence_id)
        self.assertEqual(second.due_at, self.now + 11)
        self.assertEqual(second.state, "Scheduled")
        next_claim = await self.claim(second.occurrence_id, self.now + 11)
        await self.manager.finish(next_claim.context, "Completed")
        await self.Reconciler(self.manager).tick(now=self.now + 12)
        with self.store._read_unit() as uow:
            self.assertEqual(len(self.Repository(uow).occurrences(job.job_id)), 2)

    async def test_completed_unsubmitted_occurrence_retires_known_absent_projection(self):
        job = await self.create(schedule=self.Schedule("every", 10, start_at=self.now + 1), max_runs=2)
        first = self.occurrence(job.job_id)
        claimed = await self.claim(first.occurrence_id)
        await self.manager.finish(claimed.context, "Completed")
        await self.Reconciler(self.manager).tick(now=self.now + 2)
        with self.store.read() as db:
            row = db.execute("SELECT phase,attempts,upstream_id FROM scheduler_outbox WHERE occurrence_id=?", (first.occurrence_id,)).fetchone()
            self.assertEqual(tuple(row), ("Removed", 0, None))
        self.assertEqual(self.scheduler.created, [])
        await self.Reconciler(self.manager).tick(now=self.now + 2)
        self.assertEqual(self.occurrence(job.job_id).due_at, self.now + 11)
        self.assertEqual(self.occurrence(job.job_id).state, "Scheduled")

    async def test_cancel_while_projection_gate_awaits_keeps_removed_monotonic(self):
        job = await self.create()
        original_gate = self.coordinator.gate
        async def cancel_before_return(session):
            await self.manager.cancel(job.job_id)
            return await original_gate(session)
        self.coordinator.gate = cancel_before_return
        await self.Reconciler(self.manager).tick(now=self.now)
        with self.store.read() as db:
            row = db.execute("SELECT phase,attempts,upstream_id FROM scheduler_outbox").fetchone()
            self.assertEqual(tuple(row), ("Removed", 0, None))
        self.assertEqual(self.scheduler.created, [])
        self.assertEqual(self.occurrence(job.job_id).state, "Cancelled")

    async def test_unknown_result_never_allocates_recurrence_or_clears_hazard(self):
        job = await self.create(schedule=self.Schedule("every", 10, start_at=self.now + 1))
        claimed = await self.claim(self.occurrence(job.job_id).occurrence_id)
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='uncertain' WHERE source_id='source'")
        await self.manager.finish(claimed.context, "Unknown")
        await self.Reconciler(self.manager).tick(now=self.now + 30)
        with self.store._read_unit() as uow:
            self.assertEqual(len(self.Repository(uow).occurrences(job.job_id)), 1)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT unresolved_retrieval_id FROM source_guard").fetchone()[0], "uncertain")

    async def test_periodic_reconciler_removes_acked_timer_after_scheduler_stop(self):
        job = await self.create()
        await self.Reconciler(self.manager).tick(now=self.now)
        with self.store.transaction() as uow:
            LifecycleTx(uow).disable("scheduler")
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(len(self.scheduler.removed), 1)
        self.assertEqual(self.occurrence(job.job_id).state, "Rejected")
        self.assertEqual(self.occurrence(job.job_id).disposition, "disabled")

    async def test_expiry_during_create_rejects_late_ack_using_fresh_clock(self):
        from unittest.mock import patch
        job = await self.create(expires_at=self.now + 2)
        value = [self.now]
        async def delay(projection):
            value[0] = self.now + 3
        self.scheduler.before_return = delay
        with patch("ai_secretary.jobs.time.time", side_effect=lambda: value[0]):
            await self.Reconciler(self.manager).tick()
        self.assertEqual(self.occurrence(job.job_id).disposition, "expired")
        self.assertEqual(len(self.scheduler.removed), 1)

    async def test_gate_change_while_claiming_does_not_create_authority(self):
        job = await self.create()
        self.binding["admission_id"] = "new-admission"
        result = await self.claim(self.occurrence(job.job_id).occurrence_id)
        self.assertEqual(result.disposition, "stale")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM authority_runs").fetchone()[0], 0)

    async def test_new_admission_mirror_blocks_create_with_old_gate_sample(self):
        with self.store.transaction() as uow:
            AuthorityRepository(uow).record_admission("original-session", "new-admission", "task", "new-event")
        with self.assertRaises(PermissionError):
            await self.create()

    async def test_new_admission_mirror_blocks_projection_with_old_gate_sample(self):
        job = await self.create()
        with self.store.transaction() as uow:
            AuthorityRepository(uow).record_admission("original-session", "new-admission", "task", "new-event")
        await self.Reconciler(self.manager).tick(now=self.now)
        self.assertEqual(self.scheduler.created, [])
        self.assertEqual(self.occurrence(job.job_id).disposition, "stale")

    async def test_cancelled_runner_unwinds_before_unknown_accounting(self):
        from ai_secretary.schedule_runner import run_occurrence
        job = await self.create(schedule=self.Schedule("at", self.now))
        entered, unwound = asyncio.Event(), asyncio.Event()

        class Collector:
            async def collect(inner, context, source):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    unwound.set()

        task = asyncio.create_task(run_occurrence(self.occurrence(job.job_id).occurrence_id,
                                                 store=self.store, jobs=self.manager,
                                                 coordinator=self.coordinator,
                                                 collection_factory=lambda: Collector()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(unwound.is_set())
        self.assertEqual(self.occurrence(job.job_id).state, "Unknown")

    async def test_periodic_runner_survives_transient_scheduler_unavailability(self):
        stop = asyncio.Event()
        calls = 0
        async def listing():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("Gateway briefly unavailable")
            stop.set()
            return ()
        self.scheduler.list = listing
        await self.Reconciler(self.manager).run(stop, interval=.01)
        self.assertEqual(calls, 2)

    async def test_runner_uses_original_session_and_collection_port(self):
        from ai_secretary.schedule_runner import run_occurrence
        from ai_secretary.collection import CollectionResult
        job = await self.create(schedule=self.Schedule("at", self.now))
        seen = []

        class Collector:
            async def collect(inner, context, source):
                seen.append((context.session, source.source_id))
                return CollectionResult("Completed")

        result = await run_occurrence(self.occurrence(job.job_id).occurrence_id, store=self.store,
                                      jobs=self.manager, coordinator=self.coordinator,
                                      collection_factory=lambda: Collector())
        self.assertEqual(result["status"], "Completed")
        self.assertEqual(seen, [("original-session", "source")])
        self.assertEqual(set(self.gates), {"original-session"})

    async def test_schedule_validation_rejects_unbounded_and_ambiguous_values(self):
        for args in (("cron", "* * * * *"), ("every", True), ("every", 0), ("at", float("nan")), ("at", "2026-01-01T12:00:00")):
            with self.assertRaises((ValueError, TypeError)):
                self.Schedule(*args)
        for options in ({"max_runs": True}, {"max_runs": 0}, {"max_lateness": float("inf")}, {"expires_at": self.now + 2000}):
            with self.assertRaises(ValueError):
                await self.create(**options)

    async def test_huge_numeric_values_reject_as_invalid_instead_of_overflowing(self):
        with self.assertRaises(ValueError):
            self.Schedule("every", 10 ** 500)
        with self.assertRaises(ValueError):
            self.Schedule("at", 1e300)


if __name__ == "__main__":
    unittest.main()
