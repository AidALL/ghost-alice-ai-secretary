import asyncio
from pathlib import Path
import tempfile
import unittest

from ai_secretary.authority import AuthorityStore
from ai_secretary.coordinator import AuthorityRepository, RunCoordinator
from ai_secretary.contracts import EffectSpec
from ai_secretary.desktop_owner import DesktopOwnership
from ai_secretary.ledger import LedgerTx


OBSERVATION = {"pid": 123, "process_start": "generation", "window_id": 45,
               "geometry": {"x": 1, "y": 1, "width": 100, "height": 100},
               "coordinate_space": "fixture", "backend": "builtin"}


class NativeFixture:
    timeout = 1

    def __init__(self, store):
        self.store = store
        self.effects = []
        self.observations = []
        self.outcome = {"ok": True, "effect_status": "dispatched", "result": {"dispatched": True, "verified": False}}
        self.after_observe = None

    async def call_authorized(self, command, payload, owner, **params):
        owner.validate()
        with self.store.read() as db:
            operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (payload["operation_id"],)).fetchone()
            linked = db.execute("SELECT * FROM execution_operations WHERE operation_id=?", (payload["operation_id"],)).fetchone()
            context = db.execute("SELECT * FROM execution_contexts WHERE run_id=?", (payload["run_id"],)).fetchone()
            if operation["state"] != "Applying" or linked["attempt_id"] != payload["attempt_id"] or context["desktop_fence"] != payload["lease_fence"]:
                raise AssertionError("server dispatched without atomic current ownership")
        if command == "observe":
            self.observations.append(payload.copy())
            if self.after_observe:
                self.after_observe()
            return {"ok": True, "result": {"observation": {**OBSERVATION, "token": "signed-current", "ownership": {"attempt_id": payload["attempt_id"]}}}}
        self.effects.append((command, payload.copy(), params))
        return self.outcome


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from ai_secretary.execution import DesktopExecutor, make_effect_spec
        self.make_effect_spec = make_effect_spec
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AuthorityStore(Path(self.tmp.name) / "operations.sqlite")
        with self.store.transaction() as uow:
            LedgerTx(uow).create_task("task", "user", "desktop action")
        self.binding = {"decision": "allow", "admission_id": "a", "task_id": "task", "inputEventId": "event"}

        async def gate(session):
            return dict(self.binding)

        self.coordinator = RunCoordinator(self.store, gate)
        with self.store.transaction() as uow:
            AuthorityRepository(uow).record_admission("session", "a", "task", "event")
        self.context = await self.coordinator.begin_foreground("session")
        self.native = NativeFixture(self.store)
        self.executor = DesktopExecutor(self.store, self.coordinator, self.native)
        self.spec = make_effect_spec("task", "click", {"expected": OBSERVATION, "x": 5, "y": 5})

    async def dispatch(self, operation="op"):
        return await self.executor.dispatch(self.context, operation, self.spec, "click", {"expected": OBSERVATION, "x": 5, "y": 5})

    async def test_claim_binding_and_fresh_observation_precede_input(self):
        result = await self.dispatch()
        self.assertEqual(result["state"], "Applied")
        self.assertEqual(len(self.native.effects), 1)
        _, payload, params = self.native.effects[0]
        self.assertEqual(params["expected"]["token"], "signed-current")
        self.assertEqual(payload["attempt_id"], self.native.observations[0]["attempt_id"])
        await self.coordinator.checkpoint(self.context, "read")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM desktop_lease").fetchone()[0], 0)

    async def test_replay_does_not_observe_or_send_again(self):
        await self.dispatch()
        second = await self.dispatch()
        self.assertEqual(second["state"], "cached")
        self.assertEqual(len(self.native.effects), 1)
        self.assertEqual(len(self.native.observations), 1)

    async def test_ok_unknown_backend_outcome_stays_unknown_and_blocks_replay(self):
        self.native.outcome = {"ok": True, "effect_status": "unknown", "backend_outcome": {"status": "partial"}}
        self.assertEqual((await self.dispatch())["state"], "Unknown")
        self.assertEqual((await self.dispatch())["state"], "reconcile")
        self.assertEqual(len(self.native.effects), 1)

    async def test_download_dispatch_is_unknown_until_verified_file(self):
        spec = self.make_effect_spec("task", "click", {"expected": OBSERVATION, "x": 5, "y": 5}, kind="collection_download")
        result = await self.executor.dispatch(self.context, "download", spec, "click", {"expected": OBSERVATION, "x": 5, "y": 5})
        self.assertEqual(result["state"], "Unknown")
        self.assertEqual((await self.executor.dispatch(self.context, "download", spec, "click", {"expected": OBSERVATION, "x": 5, "y": 5}))["state"], "reconcile")

    async def test_nonfinite_original_geometry_cannot_be_refreshed_into_valid_input(self):
        previous = {**OBSERVATION, "geometry": {**OBSERVATION["geometry"], "x": float("nan")}}
        with self.assertRaises(ValueError):
            await self.executor.dispatch(self.context, "invalid", self.spec, "click", {"expected": previous, "x": 5, "y": 5})
        self.assertEqual(self.native.effects, [])

    async def test_gate_change_after_observe_prevents_effect_with_no_effect_proof(self):
        self.native.after_observe = lambda: self.binding.update(decision="block")
        result = await self.dispatch()
        self.assertEqual(result["state"], "Failed")
        self.assertEqual(self.native.effects, [])
        self.assertTrue(result["result"]["no_effect"])

    async def test_identity_change_during_refresh_does_not_retarget(self):
        original = dict(OBSERVATION)
        original["process_start"] = "previous-process"
        params = {"expected": original, "x": 5, "y": 5}
        result = await self.executor.dispatch(self.context, "op", self.make_effect_spec("task", "click", params), "click", params)
        self.assertEqual(result["state"], "Failed")
        self.assertEqual(self.native.effects, [])

    async def test_gate_change_while_waiting_for_lock_is_checked_before_claim(self):
        async with DesktopOwnership(self.store.path) as owner:
            task = asyncio.create_task(self.dispatch())
            await asyncio.sleep(0.1)
            self.binding["inputEventId"] = "later"
        with self.assertRaises(PermissionError):
            await task
        self.assertEqual(self.native.effects, [])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM operations").fetchone()[0], "Prepared")

    async def test_persisted_spec_cannot_dispatch_another_command(self):
        from ai_secretary.contracts import BindingConflict
        with self.assertRaises(BindingConflict):
            await self.executor.dispatch(self.context, "mismatch", self.spec, "type", {"expected": OBSERVATION, "text": "different effect"})
        self.assertEqual(self.native.effects, [])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    async def test_persisted_spec_cannot_dispatch_changed_parameters(self):
        from ai_secretary.contracts import BindingConflict
        with self.assertRaises(BindingConflict):
            await self.executor.dispatch(self.context, "mismatch", self.spec, "click", {"expected": OBSERVATION, "x": 6, "y": 5})
        self.assertEqual(self.native.effects, [])

    async def test_caller_alias_cannot_change_inflight_modifiers(self):
        modifiers = ["shift"]
        params = {"expected": OBSERVATION, "key": "return", "modifiers": modifiers}
        spec = self.make_effect_spec("task", "hotkey", params)
        self.native.after_observe = lambda: modifiers.__setitem__(0, "command")
        result = await self.executor.dispatch(self.context, "alias", spec, "hotkey", params)
        self.assertEqual(result["state"], "Applied")
        self.assertEqual(self.native.effects[0][2]["modifiers"], ["shift"])

    async def test_core_download_baseline_runs_under_owner_before_input(self):
        seen = []
        async def baseline(context, observed):
            self.assertIsNotNone(context.desktop_fence)
            self.assertEqual(self.native.effects, [])
            seen.append(observed["result"]["observation"]["token"])
            raise ValueError("download already completed before our input")
        result = await self.executor.dispatch(self.context, "baseline", self.spec, "click",
                                             {"expected": OBSERVATION, "x": 5, "y": 5}, before_effect=baseline)
        self.assertEqual(seen, ["signed-current"])
        self.assertEqual(result["state"], "Failed")
        self.assertTrue(result["result"]["no_effect"])
        self.assertEqual(self.native.effects, [])


if __name__ == "__main__":
    unittest.main()
