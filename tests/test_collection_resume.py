import unittest
from copy import deepcopy
from types import SimpleNamespace

from ai_secretary.ledger import Ledger
from ai_secretary.operator import Operator
from ai_secretary.authority_contracts import SourceSpec
from ai_secretary.contracts import canonical_json
from ai_secretary.collection import CollectionRepository
import test_desktop_collection as desktop_fixture


class ProductionResumeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = desktop_fixture.DesktopRecipeTests.asyncSetUp
    rows = desktop_fixture.DesktopRecipeTests.rows

    async def test_delayed_file_resumes_original_operator_run_without_another_click(self):
        original = self.native.call_authorized
        async def delayed(command, payload, owner, **params):
            if command == "observe": return await original(command, payload, owner, **params)
            owner.validate()
            self.native.downloads += 1
            return {"ok": False, "effect_status": "unknown", "backend_outcome": {"state": "dispatched_unverified"}}
        self.native.call_authorized = delayed
        secretary = SimpleNamespace(ledger=SimpleNamespace(store=self.store), coordinator=self.coordinator, collection=self.collection)
        operator = Operator(secretary, None)
        first = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(first.status, "Unknown")
        self.assertEqual(self.rows("authority_runs")[0]["state"], "Pending")
        op = self.rows("operations")[0]["operation_id"]
        self.native.nodes[-1]["label"] = "Downloaded report.txt"
        (self.incoming / "report.txt").write_bytes(b"late completed download")
        second = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(second.status, "Completed")
        self.assertEqual(self.native.downloads, 1)
        self.assertEqual(len(self.rows("operations")), 1)
        self.assertEqual(self.rows("operations")[0]["operation_id"], op)
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "Verified")
        self.assertEqual(self.rows("execution_lifecycle")[0]["status"], "Running", "operator resume cannot stop its borrowed scheduler lifecycle")

    async def test_item_resume_does_not_complete_or_forget_the_other_observed_item(self):
        from test_desktop_collection import node, selector
        second = {"item_id": "appendix", "item_selector": selector("AXStaticText", "appendix", "appendix.txt"),
                  "download_selector": selector("AXButton", "appendix-download", "Download appendix.txt"),
                  "filename": "appendix.txt", "completion": {"before": selector("AXStaticText", "appendix-complete", "Not downloaded"),
                  "after": selector("AXStaticText", "appendix-complete", "Downloaded appendix.txt")}}
        self.recipe["items"].append(second)
        self.source = SourceSpec.create("source", 1, "desktop_attachments", {**self.source.scope, "desktop": self.recipe})
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET scope_json=?,scope_digest=?", (self.source.scope_json, self.source.scope_digest))
            unit.connection.execute("UPDATE collection_grants SET scope_digest=?", (self.source.scope_digest,))
        self.native.nodes.extend([node("AXStaticText", "appendix", "appendix.txt", 170), node("AXButton", "appendix-download", "Download appendix.txt", 200, actionable=True), node("AXStaticText", "appendix-complete", "Not downloaded", 230)])
        original_second_nodes = deepcopy(self.native.nodes[5:])
        async def delayed(command, payload, owner, **params):
            owner.validate()
            if command == "observe": return self.native.observed()
            self.native.downloads += 1
            return {"ok": False, "effect_status": "unknown"}
        self.native.call_authorized = delayed
        operator = Operator(SimpleNamespace(ledger=SimpleNamespace(store=self.store), coordinator=self.coordinator, collection=self.collection), None)
        self.assertEqual((await operator.collect("grant", run_id=self.context.run_id)).status, "Unknown")
        self.native.nodes[4]["label"] = "Downloaded report.txt"
        (self.incoming / "report.txt").write_bytes(b"late completed download")
        # The original second item disappears from the current UI; that cannot
        # erase it from the required result of the already-started run.
        self.native.nodes = self.native.nodes[:5]
        result = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(self.native.downloads, 1, "ingest/resume performs no new input")
        self.assertEqual(self.rows("authority_runs")[0]["state"], "Pending")
        self.assertEqual([row["item_id"] for row in self.rows("collection_run_items")], ["report", "appendix"])
        self.native.nodes.extend(original_second_nodes)
        async def next_item(command, payload, owner, **params):
            owner.validate()
            if command == "observe": return self.native.observed()
            self.assertEqual(params["element_ref"], "appendix-download-ref")
            self.native.downloads += 1
            (self.incoming / "appendix.txt").write_bytes(b"second complete file")
            self.native.nodes[-1]["label"] = "Downloaded appendix.txt"
            return {"ok": True, "effect_status": "dispatched"}
        self.native.call_authorized = next_item
        complete = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(complete.status, "Completed")
        self.assertEqual(len(complete.artifacts), 2)
        self.assertEqual(self.native.downloads, 2)
        self.assertEqual(self.rows("authority_runs")[0]["state"], "Completed")

    def two_items(self):
        from test_desktop_collection import node, selector
        self.recipe["items"].append({"item_id": "appendix", "item_selector": selector("AXStaticText", "appendix", "appendix.txt"),
            "download_selector": selector("AXButton", "appendix-download", "Download appendix.txt"), "filename": "appendix.txt",
            "completion": {"before": selector("AXStaticText", "appendix-complete", "Not downloaded"),
                           "after": selector("AXStaticText", "appendix-complete", "Downloaded appendix.txt")}})
        self.source = SourceSpec.create("source", 1, "desktop_attachments", {**self.source.scope, "desktop": self.recipe})
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET scope_json=?,scope_digest=?", (self.source.scope_json, self.source.scope_digest))
            unit.connection.execute("UPDATE collection_grants SET scope_digest=?", (self.source.scope_digest,))
        self.native.nodes.extend([node("AXStaticText", "appendix", "appendix.txt", 170),
            node("AXButton", "appendix-download", "Download appendix.txt", 200, actionable=True),
            node("AXStaticText", "appendix-complete", "Not downloaded", 230)])

    def operator(self):
        return Operator(SimpleNamespace(ledger=SimpleNamespace(store=self.store), coordinator=self.coordinator,
                                         collection=self.collection), None)

    async def test_delayed_ingest_and_later_collect_share_original_byte_budget(self):
        self.two_items()

        async def delayed(command, payload, owner, **params):
            owner.validate()
            if command == "observe": return self.native.observed()
            self.native.downloads += 1
            return {"ok": False, "effect_status": "unknown"}

        self.native.call_authorized = delayed
        operator = self.operator()
        self.assertEqual((await operator.collect("grant", run_id=self.context.run_id)).status, "Unknown")
        self.native.nodes[4]["label"] = "Downloaded report.txt"
        (self.incoming / "report.txt").write_bytes(b"a" * 700)
        first = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(first.status, "Pending")
        self.assertEqual(sum(artifact.size for artifact in first.artifacts), 700)

        async def second(command, payload, owner, **params):
            owner.validate()
            if command == "observe": return self.native.observed()
            self.assertEqual(params["element_ref"], "appendix-download-ref")
            self.native.downloads += 1
            (self.incoming / "appendix.txt").write_bytes(b"b" * 700)
            self.native.nodes[-1]["label"] = "Downloaded appendix.txt"
            return {"ok": True, "effect_status": "dispatched"}

        self.native.call_authorized = second
        result = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(result.status, "Pending", "Continuation cannot reset the original 1000-byte run limit")
        self.assertEqual(sum(row["size"] for row in self.rows("collection_artifacts")), 700)
        self.assertEqual(len(self.rows("collection_cursors")), 1)
        self.assertEqual(self.rows("authority_runs")[0]["state"], "Pending")
        self.assertIsNotNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])
        again = await operator.collect("grant", run_id=self.context.run_id)
        self.assertEqual(again.status, "Pending", "Read-only ingest also uses the remaining original quota")
        self.assertEqual(self.native.downloads, 2, "Over-quota ingestion cannot dispatch again")
        self.assertEqual(sum(row["size"] for row in self.rows("collection_artifacts")), 700)

    async def test_original_set_charges_artifact_reused_from_an_earlier_run(self):
        self.two_items()

        async def complete(command, payload, owner, **params):
            owner.validate()
            if command == "observe": return self.native.observed()
            self.native.downloads += 1
            filename, completion_index = ("report.txt", 4) if params["element_ref"] == "download-ref" else ("appendix.txt", 7)
            (self.incoming / filename).write_bytes(b"x" * 700)
            self.native.nodes[completion_index]["label"] = "Downloaded " + filename
            return {"ok": True, "effect_status": "dispatched"}

        self.native.call_authorized = complete
        first_item = (await self.port.enumerate(self.context, self.source))[0]
        with self.store.transaction() as unit:
            CollectionRepository(unit).prepare_retrieval(self.context, self.source, first_item, "prior-retrieval")
        candidate = await self.port.retrieve(self.context, self.source, "prior-retrieval", first_item)
        self.assertEqual((await self.collection.ingest(self.context, self.source, "prior-retrieval", candidate)).status, "Completed")
        await self.coordinator.finish(self.context, "Completed")
        later = await self.coordinator.begin_granted("grant", "scheduler", self.epoch)
        result = await self.operator().collect("grant", run_id=later.run_id)
        self.assertEqual(result.status, "Pending", "Reused original items consume the new run's declared byte quota")
        self.assertEqual(sum(row["size"] for row in self.rows("collection_artifacts")), 700)
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(self.rows("collection_run_items")[0]["run_id"], later.run_id)


if __name__ == "__main__": unittest.main()
