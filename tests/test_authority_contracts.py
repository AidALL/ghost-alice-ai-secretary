"""Immutable authority identities validate at construction without granting authority."""

from dataclasses import FrozenInstanceError, replace
import unittest

try:
    from ai_secretary.authority_contracts import SourceSpec, JobGrant, RunContext, ExecutionContext, GateEvidence
except ImportError:
    SourceSpec = JobGrant = RunContext = ExecutionContext = GateEvidence = None


class AuthorityContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SourceSpec, "Immutable authority contracts are missing")

    def test_source_scope_cannot_change_through_aliases(self):
        values = {"root": "/tmp/input", "selectors": {"account": "A"},
                  "recipe": "local-snapshot", "max_items": 100, "max_bytes": 67108864}
        source = SourceSpec.create("source", 1, "local_files", values)
        digest = source.scope_digest
        values["selectors"]["account"] = "B"
        source.scope["selectors"]["account"] = "C"
        self.assertEqual(source.scope["selectors"]["account"], "A")
        self.assertEqual(source.scope_digest, digest)
        with self.assertRaises(FrozenInstanceError):
            source.revision = 2

    def test_source_digest_binds_complete_canonical_scope(self):
        scope = {"selectors": {"account": "A"}, "recipe": "r", "max_items": 1,
                 "schedule": {"timezone": "UTC"}, "expires_at": 200}
        original = SourceSpec.create("s", 1, "desktop_attachments", scope)
        reordered = SourceSpec("s", 1, "desktop_attachments",
                               '{"expires_at":200,"schedule":{"timezone":"UTC"},"max_items":1,'
                               '"recipe":"r","selectors":{"account":"A"}}')
        self.assertEqual(original.scope_json, reordered.scope_json)
        self.assertEqual(original.scope_digest, reordered.scope_digest)
        for key, value in (("selectors", {"account": "B"}), ("recipe", "other"),
                           ("max_items", 2), ("schedule", {"timezone": "Europe/Berlin"}),
                           ("expires_at", 201)):
            with self.subTest(key=key):
                changed = SourceSpec.create("s", 1, "desktop_attachments", dict(scope, **{key: value}))
                self.assertNotEqual(changed.scope_digest, original.scope_digest)

    def test_source_rejects_invalid_direct_and_factory_input(self):
        for revision in (0, -1, True, 1.5):
            with self.subTest(revision=revision):
                with self.assertRaises(ValueError):
                    SourceSpec.create("s", revision, "local_files", {})
                with self.assertRaises(ValueError):
                    SourceSpec("s", revision, "local_files", "{}")
        for source_id, kind, scope_json in (("", "local_files", "{}"),
                                           ("s", "unsupported", "{}"),
                                           ("s", "local_files", '{"value":NaN}'),
                                           ("s", "local_files", "[]"),
                                           ("s", "local_files", "{bad json")):
            with self.subTest(scope=scope_json):
                with self.assertRaises((TypeError, ValueError)):
                    SourceSpec(source_id, 1, kind, scope_json)

    def grant(self, **changes):
        values = dict(grant_id="grant", source_id="source", source_revision=1, session="session",
                      admission_id="admission", task_id="task", input_event_id="input",
                      scope_digest="a" * 64, recipe="local-snapshot", expires_at=200)
        return JobGrant(**(values | changes))

    def test_grant_rejects_nonfinite_expiry_and_invalid_revision(self):
        self.assertEqual(self.grant().expires_at, 200)
        for expiry in (float("nan"), float("inf"), -float("inf"), 10 ** 400, True, "later"):
            with self.subTest(expiry=expiry):
                try:
                    self.grant(expires_at=expiry)
                except Exception as error:
                    self.assertIsInstance(error, ValueError,
                                          "Invalid expiry must be reported as boundary validation")
                else:
                    self.fail("Invalid expiry was accepted")
        for revision in (0, -1, True):
            with self.assertRaises(ValueError):
                self.grant(source_revision=revision)
        with self.assertRaises(FrozenInstanceError):
            self.grant().task_id = "other"

    def test_gate_evidence_requires_lineage_and_finite_monotonic_sample(self):
        evidence = GateEvidence("session", "admission", "task", "input", 100)
        self.assertEqual(evidence.checked_monotonic, 100)
        for sample in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                GateEvidence("session", "admission", "task", "input", sample)
        with self.assertRaises(ValueError):
            GateEvidence("session", "", "task", "input", 100)

    def context(self, **changes):
        values = dict(run_id="run", session="session", admission_id="admission", task_id="task",
                      input_event_id="input", lifecycle_id="foreground:server-owned", epoch=1,
                      run_fence=1)
        return ExecutionContext(**(values | changes))

    def test_foreground_context_needs_no_collection_configuration(self):
        context = self.context()
        self.assertFalse(hasattr(context, "source_id"))
        self.assertEqual(context.session, "session")
        fenced = replace(context, desktop_fence=2)
        self.assertEqual(fenced.run_id, context.run_id)
        self.assertIsNone(context.desktop_fence)
        with self.assertRaises(FrozenInstanceError):
            context.admission_id = "other"
        for field in ("epoch", "run_fence", "desktop_fence"):
            for value in (0, -1, True, 1.5):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.context(**{field: value})

    def collection_context(self, **changes):
        values = dict(run_id="run", session="session", admission_id="admission", task_id="task",
                      input_event_id="input", lifecycle_id="scheduler", epoch=1, run_fence=1,
                      grant_id="grant", source_id="source", source_revision=1,
                      job_id="job", job_revision=1, occurrence_id="occurrence")
        return RunContext(**(values | changes))

    def test_collection_run_requires_complete_optional_job_identity(self):
        self.assertIsInstance(self.collection_context(), ExecutionContext)
        interactive = self.collection_context(job_id=None, job_revision=None, occurrence_id=None,
                               lifecycle_id="foreground:server-owned")
        self.assertIsNone(interactive.occurrence_id)
        for changes in (dict(job_id=None), dict(job_revision=None), dict(occurrence_id=None),
                        dict(job_revision=0), dict(source_revision=True), dict(grant_id="")):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.collection_context(**changes)


if __name__ == "__main__":
    unittest.main()
