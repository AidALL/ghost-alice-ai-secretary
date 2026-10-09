"""Native contract tests. These tests never inspect or send input to an app."""
import json
import base64
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import platform
import subprocess
import sqlite3
import shutil
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NativeSourceIntegrityTests(unittest.TestCase):
    def test_unpinned_additional_vendor_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / "native"
            shutil.copytree(ROOT / "native", native)
            added = native / "Vendor/Peekaboo/Core/PeekabooAutomationKit/Sources/PeekabooAutomationKit/Unpinned.swift"
            added.write_text("// Unexpected source must not enter the selected link closure.\n")
            result = subprocess.run(["python3", str(native / "verify_sources.py")], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)


@unittest.skipUnless(platform.system() == "Darwin", "macOS native helper")
class NativeBackendRefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "desktop-secretary"
        sources = [ROOT / "native/Desktop.swift"]
        sources += [path for path in [ROOT / "native/NativeContract.swift", ROOT / "native/PeekabooBackend.swift"]
                    if path.exists()]
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                        "-framework", "AppKit", "-framework", "ApplicationServices",
                        "-framework", "ScreenCaptureKit", "-lsqlite3", *map(str, sources),
                        "-o", str(cls.binary)], check=True, capture_output=True)
        # Pure contract harness links the same production functions but never
        # calls desktop inspection or effect code.
        desktop = Path(cls.directory.name) / "DesktopFunctions.swift"
        desktop.write_text((ROOT / "native/Desktop.swift").read_text().split("@main\nstruct Desktop")[0])
        harness = Path(cls.directory.name) / "ContractHarness.swift"
        harness.write_text('''import Foundation
import AppKit
import SQLite3
@main struct Harness {
 @MainActor static func main() async {
  do {
   let input = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
   if input["mode"] as? String == "authority_connection" {
    let database = try openAuthorityDatabase(input["db_path"] as! String)
    defer { sqlite3_close(database) }
    let read = sqlite3_exec(database,"SELECT * FROM execution_contexts",nil,nil,nil)
    let mutations = ["INSERT INTO execution_lifecycle VALUES('other',1,1,'Running')",
                     "UPDATE execution_contexts SET state='Closed'",
                     "DELETE FROM execution_contexts", "CREATE TABLE extra(value TEXT)",
                     "DROP TABLE execution_contexts"]
    let refused = mutations.map { sqlite3_exec(database,$0,nil,nil,nil) }
    print(String(data: try JSONSerialization.data(withJSONObject:["read":read,"write_results":refused]),encoding:.utf8)!)
    return
   }
   if input["mode"] as? String == "observation_transaction" {
    var inspected = false
    var requests = [Any]()
    let sameBounds = CGRect(x:-100,y:20,width:100,height:60)
    func window(_ id: Int) -> [String: Any] {
     [kCGWindowNumber as String: id, kCGWindowOwnerPID as String: 456, kCGWindowBounds as String: sameBounds.dictionaryRepresentation]
    }
    let result = try await observeWindowTransaction(windowID: nil, select: { id in
     requests.append(id.map { $0 as Any } ?? NSNull())
     if inspected && input["remove_initial"] as? Bool == true && id == 11 {
      try fail("window_unavailable", "initial fixture window disappeared")
     }
     return window(id ?? (inspected ? 12 : 11))
    }, processGeneration: {
     inspected && input["change_generation"] as? Bool == true ? "2.000002" : "1.000001"
    }, collect: { initial,generation in
     let id = (initial[kCGWindowNumber as String] as! NSNumber).intValue
     inspected = true
     await Task.yield()
     return (["window":windowJSON(initial), "ax_window_id":id, "screenshot":["window_id":id]], nil)
    }, publish: { current,generation,collected,_ in
     var output = collected
     output["observation"] = ["window_id":current[kCGWindowNumber as String]!,"process_start":generation]
     output["selection_requests"] = requests
     return output
    })
    print(String(data: try JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    return
   }
   if input["mode"] as? String == "extract" {
    let graph = input["graph"] as! [String: [String]]
    let unreadable = input["unreadable"] as? [String] ?? []
    let tree = extractAXTree("root", readChildren: { unreadable.contains($0) ? nil : graph[$0] ?? [] }, readNode: { _ in [:] })
    print(String(data: try JSONSerialization.data(withJSONObject: tree), encoding: .utf8)!)
    return
   }
   if input["mode"] as? String == "string" {
    let bounded = boundedAXString(input["value"] as! String)
    print(String(data: try JSONSerialization.data(withJSONObject: ["value": bounded.value, "incomplete": bounded.incomplete]), encoding: .utf8)!)
    return
   }
   if input["mode"] as? String == "window" {
    let candidates = try (input["candidates"] as! [[String: Any]]).map { AXWindowCandidate(windowID: ($0["window_id"] as? NSNumber)?.intValue,
        bounds: try rectangle($0["geometry"] as! [String: Any])) }
    print(try chooseAXWindowIndex(candidates, windowID: input["window_id"] as! Int, bounds: rectangle(input["geometry"] as! [String: Any])))
    return
   }
   if input["mode"] as? String == "extracted_selector" {
    func sidecar(_ value: String) throws -> [String: Any] {
     let bounded = boundedAXString(value)
     let candidate: [String: Any] = ["role":"AXButton", "identifier":"download", "label":bounded.value,
      "geometry":["x":0,"y":0,"width":20,"height":10], "enabled":true,"actionable":true,"visible":true]
     return try selectorSidecar([candidate], truncated: bounded.incomplete)
    }
    let expected = try sidecar(input["before"] as! String)
    let fresh = try sidecar(input["after"] as! String)
    let ref = (expected["refs"] as! [[String: Any]])[0]["ref"] as! String
    _ = try corroborateSelector(ref, expected: expected, fresh: fresh)
    print("matched")
    return
   }
   let point = try corroborateSelector(input["ref"] as! String, expected: input["expected"] as! [String: Any], fresh: input["fresh"] as! [String: Any])
   print("matched \\(point.x) \\(point.y)")
  } catch let error as DesktopError { print(error.code) }
    catch { print("unexpected-error") }
 }
}''')
        cls.contract_binary = Path(cls.directory.name) / "contract"
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                        "-framework", "AppKit", "-framework", "ApplicationServices",
                        "-framework", "ScreenCaptureKit", "-lsqlite3", str(desktop),
                        str(ROOT / "native/NativeContract.swift"), str(ROOT / "native/PeekabooBackend.swift"),
                        str(harness), "-o", str(cls.contract_binary)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def call(self, pass_fds=(), **request):
        result = subprocess.run([str(self.binary)], input=json.dumps(request), text=True,
                                capture_output=True, check=True, timeout=10, pass_fds=pass_fds)
        return json.loads(result.stdout)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.state.chmod(0o700)
        self.key = self.state / "native-observation.key"
        self.key.write_bytes(b"n" * 32)
        self.key.chmod(0o600)
        self.db = self.state / "operations.sqlite"
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        self.db.chmod(0o600)
        self.connection.executescript('''
          CREATE TABLE execution_contexts(run_id TEXT,session TEXT,admission_id TEXT,task_id TEXT,
           input_event_id TEXT,lifecycle_id TEXT,epoch INTEGER,run_fence INTEGER,state TEXT,desktop_fence INTEGER);
          CREATE TABLE execution_lifecycle(lifecycle_id TEXT,epoch INTEGER,enabled INTEGER,status TEXT);
          CREATE TABLE operations(operation_id TEXT,task_id TEXT,state TEXT,attempt_id TEXT);
          CREATE TABLE execution_operations(operation_id TEXT,run_id TEXT,attempt_id TEXT);
          CREATE TABLE execution_admissions(session TEXT PRIMARY KEY,admission_id TEXT,task_id TEXT,input_event_id TEXT);
          CREATE TABLE desktop_lease(singleton INTEGER,owner TEXT,fence INTEGER,boot_id TEXT,expires REAL);
          CREATE TABLE authority_runs(run_id TEXT,grant_id TEXT,source_id TEXT,source_revision INTEGER,
           lifecycle_id TEXT,epoch INTEGER,run_fence INTEGER,state TEXT);
          CREATE TABLE source_guard(source_id TEXT,revoked INTEGER,current_revision INTEGER,active_run_id TEXT,run_fence INTEGER,unresolved_retrieval_id TEXT);
          CREATE TABLE authority_sources(source_id TEXT,revision INTEGER,scope_digest TEXT);
          CREATE TABLE collection_grants(grant_id TEXT,source_id TEXT,source_revision INTEGER,session TEXT,admission_id TEXT,
           task_id TEXT,input_event_id TEXT,scope_digest TEXT,expires_at REAL,revoked INTEGER);
          CREATE TABLE authority_retrievals(retrieval_id TEXT,run_id TEXT,operation_id TEXT,state TEXT);
          INSERT INTO execution_contexts VALUES ('run','session','admission','task','input','life',1,2,'Active',3);
          INSERT INTO execution_lifecycle VALUES ('life',1,1,'Running');
          INSERT INTO operations VALUES ('operation','task','Applying','attempt');
          INSERT INTO execution_operations VALUES ('operation','run','attempt');
          INSERT INTO execution_admissions VALUES ('session','admission','task','input');
        ''')
        boot = subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True).strip()
        self.connection.execute("INSERT INTO desktop_lease VALUES (1,'owner',3,?,?)", (boot, time.monotonic() + 60))
        self.connection.commit()
        self.lock = os.open(self.state / "desktop-dispatch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.lock)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.payload = dict(db_path=str(self.db), run_id="run", session="session", admission_id="admission",
                            task_id="task", input_event_id="input", lifecycle_id="life", epoch=1, run_fence=2,
                            operation_id="operation", attempt_id="attempt", lease_owner="owner", lease_fence=3,
                            lease_boot_id=boot, dispatch_lock_fd=self.lock)

    def authorized(self, **updates):
        payload = {**self.payload, **updates}
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        envelope = {"payload": base64.b64encode(data).decode(),
                    "token": hmac.new(self.key.read_bytes(), data, hashlib.sha256).hexdigest()}
        return self.call(command="status", key_path=str(self.key), _authorization=envelope, pass_fds=(self.lock,))

    def test_status_names_actual_backend_and_authority_contract(self):
        result = self.call(command="status")["result"]
        self.assertEqual(result.get("backend"), "builtin")
        self.assertEqual(result.get("backend_version"), "1")
        self.assertEqual(result.get("authorization_contract"), "local-sqlite-v1")
        self.assertIn("click", result.get("capabilities", []))

    def test_private_authorization_is_validated_before_os_observation(self):
        result = self.call(command="click", key_path=str(self.key), _authorization={"epoch": -1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "authorization_invalid")
        self.assertEqual(result["effect_status"], "not_started")

    def test_unsigned_effect_is_refused(self):
        result = self.call(command="click")
        self.assertEqual(result["error"]["code"], "authorization_required")
        self.assertEqual(result["effect_status"], "not_started")

    def test_signed_current_context_and_inherited_lock_are_accepted(self):
        result = self.authorized()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["effect_status"], "not_started")

    def test_authority_connection_blocks_row_and_schema_mutations(self):
        self.connection.close()
        before = self.db.read_bytes()
        result = json.loads(self.extraction(mode="authority_connection", db_path=str(self.db)))
        self.assertEqual(result["read"], sqlite3.SQLITE_OK)
        self.assertEqual(result["write_results"], [sqlite3.SQLITE_READONLY] * 5)
        self.assertEqual(self.db.read_bytes(), before)

    def test_payload_tampering_does_not_create_authority(self):
        original = json.dumps(self.payload, separators=(",", ":")).encode()
        changed = json.dumps({**self.payload, "run_id": "another-run"}, separators=(",", ":")).encode()
        envelope = dict(payload=base64.b64encode(changed).decode(),
                        token=hmac.new(self.key.read_bytes(), original, hashlib.sha256).hexdigest())
        result = self.call(command="click", key_path=str(self.key), _authorization=envelope, pass_fds=(self.lock,))
        self.assertEqual(result["error"]["code"], "authorization_invalid")
        self.assertEqual(result["effect_status"], "not_started")

    def test_current_admission_mirror_refuses_old_context_even_if_active(self):
        for mutation in ["UPDATE execution_admissions SET admission_id='new-admission'",
                         "UPDATE execution_admissions SET task_id='new-task'",
                         "UPDATE execution_admissions SET input_event_id='new-input'",
                         "DELETE FROM execution_admissions"]:
            with self.subTest(mutation=mutation):
                self.connection.execute(mutation)
                self.connection.commit()
                result = self.authorized()
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], "authorization_revoked")
                self.assertEqual(result["effect_status"], "not_started")
                self.connection.execute("DELETE FROM execution_admissions")
                self.connection.execute("INSERT INTO execution_admissions VALUES ('session','admission','task','input')")
                self.connection.commit()

    @unittest.skipUnless(os.environ.get("AI_SECRETARY_PEEKABOO_BINARY"), "optional built product explicitly selected")
    def test_optional_product_context_and_refusal(self):
        self.binary = Path(os.environ["AI_SECRETARY_PEEKABOO_BINARY"]).resolve()
        status = self.call(command="status")["result"]
        self.assertEqual(status["backend"], "peekaboo")
        self.assertEqual(status["backend_version"], "4.6.0")
        self.assertNotIn("drag", status["capabilities"])
        result = self.call(command="click")
        self.assertEqual(result["error"]["code"], "authorization_required")
        self.assertEqual(result["effect_status"], "not_started")
        self.assertTrue(self.authorized()["ok"])

    def test_final_database_predicates_refuse_stale_authority(self):
        mutations = [
            "UPDATE execution_lifecycle SET enabled=0", "UPDATE execution_lifecycle SET epoch=2",
            "UPDATE execution_contexts SET state='Draining'", "UPDATE execution_contexts SET run_fence=9",
            "UPDATE execution_contexts SET desktop_fence=9", "UPDATE execution_contexts SET input_event_id='other'",
            "UPDATE operations SET state='Unknown'", "UPDATE operations SET attempt_id='other'",
            "UPDATE execution_operations SET run_id='other'", "UPDATE desktop_lease SET fence=9",
            "UPDATE desktop_lease SET owner='other'", "UPDATE desktop_lease SET expires=0",
        ]
        for sql in mutations:
            with self.subTest(sql=sql):
                self.connection.execute("SAVEPOINT mutation")
                self.connection.execute(sql)
                # Native reads committed state; restore fixture via inverse full
                # snapshot instead of keeping a transaction around subprocess IO.
                self.connection.execute("RELEASE mutation")
                result = self.authorized()
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], "authorization_revoked")
                self.assertEqual(result["effect_status"], "not_started")
                self.connection.execute("UPDATE execution_lifecycle SET enabled=1,epoch=1")
                self.connection.execute("UPDATE execution_contexts SET state='Active',run_fence=2,desktop_fence=3,input_event_id='input'")
                self.connection.execute("UPDATE operations SET state='Applying',attempt_id='attempt'")
                self.connection.execute("UPDATE execution_operations SET run_id='run'")
                self.connection.execute("UPDATE desktop_lease SET fence=3,owner='owner',expires=?", (time.monotonic()+60,))
                self.connection.commit()

    def test_signed_boot_receipt_is_compared_to_actual_boot(self):
        self.connection.execute("UPDATE desktop_lease SET boot_id='stale boot'")
        self.connection.commit()
        result = self.authorized(lease_boot_id="stale boot")
        self.assertEqual(result["error"]["code"], "authorization_revoked")

    def collection(self):
        for name in ["authority_runs", "source_guard", "authority_sources", "collection_grants"]:
            self.connection.execute(f"DELETE FROM {name}")
        self.connection.execute("INSERT INTO authority_runs VALUES ('run','grant','source',1,'life',1,2,'Running')")
        self.connection.execute("INSERT INTO source_guard VALUES ('source',0,1,'run',2,NULL)")
        self.connection.execute("INSERT INTO authority_sources VALUES ('source',1,'scope')")
        self.connection.execute("INSERT INTO collection_grants VALUES ('grant','source',1,'session','admission','task','input','scope',?,0)",
                                (time.time()+60,))
        self.connection.commit()

    def test_collection_source_and_grant_are_current_at_native_acceptance(self):
        self.collection()
        self.assertTrue(self.authorized()["ok"])
        for mutation in ["UPDATE collection_grants SET expires_at=0", "UPDATE collection_grants SET revoked=1",
                         "UPDATE collection_grants SET input_event_id='other'", "UPDATE collection_grants SET scope_digest='other'",
                         "UPDATE source_guard SET revoked=1", "UPDATE source_guard SET current_revision=2",
                         "UPDATE source_guard SET active_run_id='other'", "UPDATE source_guard SET run_fence=9",
                         "UPDATE authority_runs SET run_fence=9", "UPDATE authority_runs SET source_revision=2"]:
            with self.subTest(mutation=mutation):
                self.collection()
                self.connection.execute(mutation)
                self.connection.commit()
                result = self.authorized()
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"]["code"], "authorization_revoked")
                self.assertEqual(result["effect_status"], "not_started")

    def test_unresolved_collection_retrieval_must_belong_to_exact_run_and_operation(self):
        self.collection()
        self.connection.execute("INSERT INTO authority_retrievals VALUES ('retrieval','run','operation','Applying')")
        self.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='retrieval'")
        self.connection.commit()
        self.assertTrue(self.authorized()["ok"])
        self.connection.execute("UPDATE authority_retrievals SET operation_id='other'")
        self.connection.commit()
        result = self.authorized()
        self.assertEqual(result["error"]["code"], "authorization_revoked")
        self.assertEqual(result["effect_status"], "not_started")

    def test_inherited_fd_without_exclusive_lock_is_refused(self):
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        result = self.authorized()
        self.assertEqual(result["error"]["code"], "authorization_invalid")

    def test_exact_selector_corroboration_is_unique_and_bounded(self):
        ref = dict(ref="opaque", role="AXButton", identifier="download", label="Download",
                   geometry=dict(x=-100, y=25, width=60, height=20), enabled=True, actionable=True, visible=True)
        expected = dict(schema="secretary-selectors-v1", refs=[ref], truncated=False)
        fresh_ref = {**ref, "ref": "fresh"}
        fresh = {**expected, "refs": [fresh_ref]}
        def match(old, new):
            return subprocess.check_output([str(self.contract_binary)], input=json.dumps(
                dict(ref="opaque", expected=old, fresh=new)), text=True).strip()
        self.assertEqual(match(expected, fresh), "matched -70.0 35.0")
        self.assertEqual(match(expected, {**fresh, "refs": [fresh_ref, {**fresh_ref, "ref": "duplicate"}]}), "selector_mismatch")
        self.assertEqual(match(expected, {**fresh, "refs": [{**fresh_ref, "label": "download"}]}), "selector_mismatch")
        self.assertEqual(match(expected, {**fresh, "refs": [{**fresh_ref, "geometry": {**ref["geometry"], "x": -99.9}}]}), "selector_mismatch")
        self.assertEqual(match(expected, {**fresh, "truncated": True}), "selector_unavailable")
        self.assertEqual(match({**expected, "schema": "unknown"}, fresh), "selector_invalid")
        self.assertEqual(match(expected, {**fresh, "refs": [{**fresh_ref, "attributes": {"arbitrary": "content"}}]}), "selector_invalid")
        self.assertEqual(match(expected, {**fresh, "refs": [{**fresh_ref, "label": "x"*4097}]}), "selector_invalid")
        self.assertEqual(match(expected, {**fresh, "refs": [{**fresh_ref, "enabled": 1}]}), "selector_invalid")
        self.assertEqual(match(expected, {**fresh, "refs": [{**fresh_ref, "ref": str(i)} for i in range(129)]}), "selector_invalid")

    def extraction(self, **request):
        return subprocess.check_output([str(self.contract_binary)], input=json.dumps(request), text=True).strip()

    def test_ax_extraction_marks_depth_cap_incomplete(self):
        graph = {"root": ["0"], **{str(i): [str(i+1)] for i in range(7)}}
        tree = json.loads(self.extraction(mode="extract", graph=graph))
        self.assertEqual(len(tree["nodes"]), 7)
        self.assertTrue(tree["truncated"], "children beyond depth6 must invalidate selector completeness")

    def test_ax_extraction_accepts_complete_boundary_trees_and_marks_unreadable_branches(self):
        for graph in [{"root": [str(i) for i in range(249)]},
                      {"root": ["0"], **{str(i): [str(i+1)] for i in range(5)}}]:
            self.assertFalse(json.loads(self.extraction(mode="extract", graph=graph))["truncated"])
        tree = json.loads(self.extraction(mode="extract", graph={"root": ["unreadable"]}, unreadable=["unreadable"]))
        self.assertTrue(tree["truncated"])

    def test_ax_extraction_marks_child_prefix_and_node_caps_incomplete(self):
        for graph in [{"root": [str(i) for i in range(250)]},
                      {"root": [str(i) for i in range(300)]},
                      {"root": ["first", "second"], "first": [str(i) for i in range(248)],
                       "second": ["last"]}]:
            with self.subTest(graph_size=len(graph["root"])):
                tree = json.loads(self.extraction(mode="extract", graph=graph))
                self.assertEqual(len(tree["nodes"]), 250)
                self.assertTrue(tree["truncated"])

    def test_ax_extraction_preserves_entire_bounded_strings(self):
        for value in ["a"*4050, "a"*4096, "한"*1365]:
            with self.subTest(bytes=len(value.encode())):
                result = json.loads(self.extraction(mode="string", value=value))
                self.assertEqual(result["value"], value)
                self.assertFalse(result["incomplete"])
        result = json.loads(self.extraction(mode="string", value="a"*4097))
        self.assertTrue(result["incomplete"])

    def test_extracted_selector_detects_suffix_change_and_refuses_oversize_label(self):
        before = "a"*4050
        self.assertEqual(self.extraction(mode="extracted_selector", before=before, after=before), "matched")
        self.assertEqual(self.extraction(mode="extracted_selector", before=before, after=before[:-1]+"b"), "selector_mismatch")
        self.assertEqual(self.extraction(mode="extracted_selector", before="a"*4097, after="a"*4097), "selector_unavailable")

    def test_ax_window_extraction_requires_unique_exact_id_and_bounds(self):
        shape = dict(x=-100, y=25, width=60, height=20)
        def select(candidates):
            return self.extraction(mode="window", window_id=11, geometry=shape, candidates=candidates)
        one = dict(window_id=11, geometry=shape)
        other = dict(window_id=12, geometry=shape)
        self.assertEqual(select([other, one]), "1")
        self.assertEqual(select([one, one]), "ax_window_ambiguous")
        self.assertEqual(select([dict(geometry=shape)]), "ax_window_unavailable")
        self.assertEqual(select([other]), "ax_window_unavailable")
        self.assertEqual(select([{**one, "geometry": {**shape, "x": -98}}]), "ax_window_unavailable")

    def test_observation_transaction_pins_initial_window_through_async_reordering(self):
        result = json.loads(self.extraction(mode="observation_transaction"))
        self.assertEqual(result["window"]["window_id"], 11)
        self.assertEqual(result["ax_window_id"], 11)
        self.assertEqual(result["screenshot"]["window_id"], 11)
        self.assertEqual(result["observation"]["window_id"], 11)
        self.assertEqual(result["selection_requests"], [None, 11])

    def test_observation_transaction_refuses_reused_process_generation(self):
        self.assertEqual(self.extraction(mode="observation_transaction", change_generation=True), "process_mismatch")

    def test_observation_transaction_refuses_removed_initial_window(self):
        self.assertEqual(self.extraction(mode="observation_transaction", remove_initial=True), "window_unavailable")

    def test_selected_backend_cannot_silently_use_builtin(self):
        result = self.call(command="click", backend="peekaboo")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "backend_mismatch")
        self.assertEqual(result["effect_status"], "not_started")


@unittest.skipUnless(platform.system() == "Darwin", "macOS native helper")
class NativeRealStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_products_accept_real_authority_store_and_server_transport(self):
        from ai_secretary.authority import AuthorityStore
        from ai_secretary.coordinator import AuthorityRepository
        from ai_secretary.desktop_owner import DesktopOwnership
        from ai_secretary.ledger import Ledger, _boot_id
        from ai_secretary.native import NativeClient

        products = [("builtin", ROOT / "build/desktop-secretary")]
        if os.environ.get("AI_SECRETARY_PEEKABOO_BINARY"):
            products.append(("peekaboo", Path(os.environ["AI_SECRETARY_PEEKABOO_BINARY"]).resolve()))
        with tempfile.TemporaryDirectory(prefix="secretary-native-real-store-") as directory:
            state = Path(directory)
            state.chmod(0o700)
            db = state / "operations.sqlite"
            store = AuthorityStore(db)
            async with DesktopOwnership(db) as owner:
                ledger = Ledger(db)
                ledger.create_task("fixture-task", "fixture-owner", {})
                fence = ledger.acquire_lease("fixture-owner", ttl=600)
                with store.transaction() as unit:
                    AuthorityRepository.initialize(unit)
                    AuthorityRepository(unit).record_admission("fixture-session", "fixture-admission", "fixture-task", "fixture-input")
                    unit.connection.execute("INSERT INTO execution_lifecycle VALUES('fixture-life',1,1,'Running')")
                    unit.connection.execute("""INSERT INTO execution_contexts
                        VALUES('fixture-run','fixture-session','fixture-admission','fixture-task',
                               'fixture-input','fixture-life',1,1,?,'Active')""", (fence,))
                    unit.connection.execute("""INSERT INTO operations
                        (operation_id,task_id,owner,effect_json,fingerprint,state,attempt_id)
                        VALUES('fixture-operation','fixture-task','fixture-owner','{}','fixture','Applying','fixture-attempt')""")
                    unit.connection.execute("INSERT INTO execution_operations VALUES('fixture-operation','fixture-run','fixture-attempt')")
                payload = dict(db_path=str(db), run_id="fixture-run", session="fixture-session",
                               admission_id="fixture-admission", task_id="fixture-task", input_event_id="fixture-input",
                               lifecycle_id="fixture-life", epoch=1, run_fence=1, operation_id="fixture-operation",
                               attempt_id="fixture-attempt", lease_owner="fixture-owner", lease_fence=fence,
                               lease_boot_id=_boot_id(), dispatch_lock_fd=owner.fd)
                self.assertFalse(Path(str(db) + "-wal").exists(), "exercise WAL startup after the store closes")
                self.assertFalse(Path(str(db) + "-shm").exists(), "exercise WAL startup without a prior reader")
                before = db.read_bytes()
                for backend, binary in products:
                    with self.subTest(backend=backend):
                        self.assertTrue(binary.is_file(), "build the requested native product before this regression")
                        client = NativeClient(binary, state_dir=state, backend=backend)
                        result = await client.call_authorized("status", payload, owner)
                        self.assertTrue(result["ok"], result)
                        self.assertEqual(result["result"]["backend"], backend)
                        self.assertEqual(result["effect_status"], "not_started")
                        self.assertEqual(db.read_bytes(), before, "native validation must not alter authority records")


if __name__ == "__main__":
    unittest.main()
