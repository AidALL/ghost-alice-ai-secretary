"""Behavior tests for the durable effect boundary, using actual SQLite files."""

import dataclasses
from contextlib import closing
import inspect
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


try:
    from ai_secretary.contracts import (
        ApprovalRequired, BindingConflict, EffectSpec, LeaseBusy, content_digest,
    )
    from ai_secretary.ledger import Ledger
except ModuleNotFoundError:
    Ledger = None


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Ledger, "The durable ledger implementation is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.create_task("task", "user:ray", "Send the reviewed draft")
        self.spec = EffectSpec("task", "send", {"account": "ray", "recipient": "kim"},
                               {"body": "Hello", "attachments": ["sha256:file"]})

    def prepare(self, operation="op", spec=None):
        self.ledger.prepare(operation, spec or self.spec)

    def test_effect_fingerprint_is_canonical_and_payload_cannot_drift(self):
        payload = {"body": "Hello", "attachments": []}
        spec = EffectSpec("task", "send", {"recipient": "kim", "account": "ray"}, payload)
        same = EffectSpec("task", "send", {"account": "ray", "recipient": "kim"},
                          {"attachments": [], "body": "Hello"})
        self.assertEqual(spec.fingerprint, same.fingerprint)
        payload["body"] = "mutated"
        spec.payload["attachments"].append("injected")
        self.assertEqual(spec.fingerprint, same.fingerprint)
        self.assertEqual(spec.payload, {"attachments": [], "body": "Hello"})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.kind = "delete"

    def test_non_json_and_non_finite_values_rejected_before_binding(self):
        for invalid in (float("nan"), float("inf"), {1: "integer key"}, {"x": object()}):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises((TypeError, ValueError)):
                    EffectSpec("task", "send", "kim", invalid)

    def test_task_owner_and_intent_are_bound(self):
        self.ledger.create_task("task", "user:ray", "Send the reviewed draft")
        for owner, intent in (("user:other", "Send the reviewed draft"), ("user:ray", "Delete")):
            with self.assertRaises(BindingConflict):
                self.ledger.create_task("task", owner, intent)

    def test_prepare_requires_existing_task(self):
        with self.assertRaises(BindingConflict):
            self.prepare(spec=EffectSpec("missing", "send", "kim", {"body": "Hello"}))

    def test_replay_rejects_task_recipient_account_body_attachment_changes(self):
        self.prepare()
        self.ledger.create_task("other", "user:ray", "Another task")
        variants = [
            EffectSpec("other", "send", self.spec.target, self.spec.payload),
            EffectSpec("task", "delete", self.spec.target, self.spec.payload),
            EffectSpec("task", "send", {"account": "else", "recipient": "kim"}, self.spec.payload),
            EffectSpec("task", "send", {"account": "ray", "recipient": "else"}, self.spec.payload),
            EffectSpec("task", "send", self.spec.target, {"body": "Changed", "attachments": ["sha256:file"]}),
            EffectSpec("task", "send", self.spec.target, {"body": "Hello", "attachments": ["sha256:other"]}),
        ]
        for variant in variants:
            with self.subTest(fingerprint=variant.fingerprint):
                with self.assertRaises(BindingConflict):
                    self.prepare(spec=variant)
        self.prepare()
        self.assertEqual(self.ledger.claim("op")["state"], "claimed")

    def test_applied_replay_returns_durable_cached_result(self):
        self.prepare()
        attempt = self.ledger.claim("op")
        self.assertEqual(attempt["state"], "claimed")
        self.ledger.finish("op", attempt["attempt_id"], "Applied", {"message_id": "123"})
        reopened = Ledger(self.path)
        reopened.prepare("op", self.spec)
        self.assertEqual(reopened.claim("op"), {"state": "cached", "result": {"message_id": "123"}})

    def test_concurrent_connections_have_one_claim_winner(self):
        self.prepare()
        ledgers = [Ledger(self.path) for _ in range(8)]
        barrier = threading.Barrier(len(ledgers))

        def claim(ledger):
            barrier.wait(timeout=5)
            return ledger.claim("op")

        with ThreadPoolExecutor(max_workers=len(ledgers)) as pool:
            outcomes = list(pool.map(claim, ledgers))
        self.assertEqual(sum(x["state"] == "claimed" for x in outcomes), 1)
        self.assertEqual(sum(x["state"] == "reconcile" for x in outcomes), 7)

    def test_simultaneous_first_open_creates_one_usable_database(self):
        # Repeat the real filesystem race because the stat/open gap is narrow.
        for iteration in range(30):
            with self.subTest(iteration=iteration):
                path = Path(self.tmp.name) / f"first-open-{iteration}.sqlite3"
                barrier = threading.Barrier(12)

                def open_ledger(index):
                    barrier.wait(timeout=5)
                    try:
                        ledger = Ledger(path)
                        ledger.create_task(f"task-{index}", "user:ray", "Read")
                        return ledger
                    except Exception as error:
                        return error

                with ThreadPoolExecutor(max_workers=12) as pool:
                    ledgers = list(pool.map(open_ledger, range(12)))
                self.assertEqual(len(ledgers), 12)
                self.assertFalse([result for result in ledgers if isinstance(result, Exception)],
                                 "Simultaneous first open must not fail a worker")
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 12)

    def test_crash_recovery_never_retries_uncertain_effect(self):
        self.prepare()
        attempt = self.ledger.claim("op")
        reopened = Ledger(self.path)
        self.assertEqual(reopened.recover_inflight(), 1)
        self.assertEqual(reopened.recover_inflight(), 0)
        for _ in range(2):
            self.assertEqual(reopened.claim("op")["state"], "reconcile")
        with self.assertRaises(BindingConflict):
            self.ledger.finish("op", attempt["attempt_id"], "Applied", {"late": True})

    def test_wrong_attempt_cannot_finish_or_overwrite_terminal_result(self):
        self.prepare()
        attempt = self.ledger.claim("op")
        with self.assertRaises(BindingConflict):
            self.ledger.finish("op", "foreign", "Applied", {})
        self.ledger.finish("op", attempt["attempt_id"], "Applied", {"ok": True})
        with self.assertRaises(BindingConflict):
            self.ledger.finish("op", attempt["attempt_id"], "Applied", {"ok": False})
        self.assertEqual(self.ledger.claim("op")["result"], {"ok": True})

    def test_timeout_failed_is_unknown_and_not_retryable(self):
        self.prepare()
        attempt = self.ledger.claim("op")
        self.ledger.finish("op", attempt["attempt_id"], "Failed", {"error": "timeout"})
        self.assertEqual(self.ledger.claim("op")["state"], "reconcile")

    def test_proven_no_effect_failure_can_retry_with_new_attempt(self):
        self.prepare()
        first = self.ledger.claim("op")
        self.ledger.finish("op", first["attempt_id"], "Failed",
                           {"no_effect": True, "reason": "rejected before dispatch"})
        second = self.ledger.claim("op")
        self.assertEqual(second["state"], "claimed")
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        with self.assertRaises(BindingConflict):
            self.ledger.finish("op", first["attempt_id"], "Applied", {})

    def test_invalid_finish_is_atomic_and_cannot_discard_attempt(self):
        self.prepare()
        attempt = self.ledger.claim("op")
        for status, result in (("Prepared", {}), ("Applied", {"bad": math.nan})):
            with self.assertRaises(ValueError):
                self.ledger.finish("op", attempt["attempt_id"], status, result)
        self.ledger.finish("op", attempt["attempt_id"], "Applied", {"valid": True})
        self.assertEqual(self.ledger.claim("op")["state"], "cached")

    def test_unknown_status_is_never_retryable(self):
        self.prepare()
        attempt = self.ledger.claim("op")
        self.ledger.finish("op", attempt["attempt_id"], "Unknown", {"uncertain": True})
        self.assertEqual(self.ledger.claim("op")["state"], "reconcile")

    def test_draft_approval_is_exact_and_durable(self):
        draft = {"account": "ray", "recipient": "kim", "body": "Hello", "attachments": []}
        digest = self.ledger.create_draft("task", "draft", draft)
        self.assertEqual(digest, content_digest(draft))
        with self.assertRaises(ApprovalRequired):
            self.ledger.validate_approval("draft", draft)
        with self.assertRaises(ApprovalRequired):
            self.ledger.approve("draft", "wrong digest", "user:ray")
        self.ledger.approve("draft", digest, "user:ray")
        self.assertTrue(Ledger(self.path).validate_approval("draft", draft))
        reordered = {"attachments": [], "body": "Hello", "recipient": "kim", "account": "ray"}
        self.assertTrue(self.ledger.validate_approval("draft", reordered))

    def test_draft_edit_invalidates_approval_and_prevents_task_rebinding(self):
        draft = {"account": "ray", "recipient": "kim", "body": "Hello", "attachments": []}
        digest = self.ledger.create_draft("task", "draft", draft)
        self.ledger.approve("draft", digest, "user:ray")
        for key, changed in (("account", "other"), ("recipient", "other"),
                             ("body", "changed"), ("attachments", ["sha256:other"])):
            edit = dict(draft, **{key: changed})
            with self.assertRaises(ApprovalRequired):
                self.ledger.validate_approval("draft", edit)
        self.ledger.create_draft("task", "draft", dict(draft, body="changed"))
        with self.assertRaises(ApprovalRequired):
            self.ledger.validate_approval("draft", draft)
        with self.assertRaises(ApprovalRequired):
            self.ledger.validate_approval("draft", dict(draft, body="changed"))
        self.ledger.create_task("other", "user:ray", "Other")
        with self.assertRaises(BindingConflict):
            self.ledger.create_draft("other", "draft", draft)

    def test_approved_draft_cannot_authorize_another_task(self):
        self.assertIn("task_id", inspect.signature(self.ledger.validate_approval).parameters,
                      "Approval validation must support an explicit current task binding")
        draft = {"account": "ray", "recipient": "kim", "body": "Hello", "attachments": []}
        digest = self.ledger.create_draft("task", "draft", draft)
        self.ledger.approve("draft", digest, "user:ray")
        self.ledger.create_task("other", "user:ray", "Another task")
        with self.assertRaises(ApprovalRequired):
            self.ledger.validate_approval("draft", draft, task_id="other")
        self.assertTrue(self.ledger.validate_approval("draft", draft, task_id="task"))

    def test_missing_draft_and_empty_actor_cannot_be_approved(self):
        with self.assertRaises(ApprovalRequired):
            self.ledger.approve("missing", "digest", "user:ray")
        digest = self.ledger.create_draft("task", "draft", {"body": "Hello"})
        with self.assertRaises(ApprovalRequired):
            self.ledger.approve("draft", digest, "")

    def test_active_lease_blocks_other_connections_and_owner_overlap(self):
        first = self.ledger.acquire_lease("worker-a", ttl=10)
        self.assertTrue(self.ledger.assert_lease("worker-a", first))
        other = Ledger(self.path)
        for owner in ("worker-a", "worker-b"):
            with self.assertRaises(LeaseBusy):
                other.acquire_lease(owner)
        with self.assertRaises(LeaseBusy):
            other.assert_lease("worker-b", first)
        self.ledger.release_lease("worker-a", first)
        second = other.acquire_lease("worker-b")
        self.assertGreater(second, first)
        with self.assertRaises(LeaseBusy):
            self.ledger.release_lease("worker-a", first)
        self.assertTrue(other.assert_lease("worker-b", second))

    def test_expired_lease_is_fenced_with_persistently_increasing_generation(self):
        first = self.ledger.acquire_lease("old", ttl=0.03)
        time.sleep(0.05)
        with self.assertRaises(LeaseBusy):
            self.ledger.assert_lease("old", first)
        reopened = Ledger(self.path)
        second = reopened.acquire_lease("new")
        self.assertGreater(second, first)
        with self.assertRaises(LeaseBusy):
            self.ledger.assert_lease("old", first)
        reopened.release_lease("new", second)
        self.assertGreater(Ledger(self.path).acquire_lease("last"), second)

    def test_concurrent_lease_acquisition_has_one_winner(self):
        ledgers = [Ledger(self.path) for _ in range(8)]
        barrier = threading.Barrier(8)

        def acquire(index):
            barrier.wait(timeout=5)
            try:
                return ledgers[index].acquire_lease(f"worker-{index}")
            except LeaseBusy:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            fences = list(pool.map(acquire, range(8)))
        self.assertEqual([fence for fence in fences if fence is not None], [1])

    def test_boolean_fence_cannot_impersonate_first_generation(self):
        fence = self.ledger.acquire_lease("worker")
        self.assertEqual(fence, 1)
        with self.assertRaises(LeaseBusy):
            self.ledger.assert_lease("worker", True)
        with self.assertRaises(LeaseBusy):
            self.ledger.release_lease("worker", True)
        self.assertTrue(self.ledger.assert_lease("worker", fence))

    def test_reboot_identity_invalidates_monotonic_lease(self):
        first = self.ledger.acquire_lease("old")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE desktop_lease SET boot_id = ?", ("prior-boot",))
            connection.commit()
        with self.assertRaises(LeaseBusy):
            self.ledger.assert_lease("old", first)
        self.assertGreater(Ledger(self.path).acquire_lease("new"), first)

    def test_invalid_ttl_cannot_allocate_fence(self):
        for ttl in (0, -1, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                self.ledger.acquire_lease("worker", ttl=ttl)
        self.assertEqual(self.ledger.acquire_lease("worker"), 1)


if __name__ == "__main__":
    unittest.main()
