"""Same-file authority and ledger composition, exercised with real SQLite files."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_secretary.contracts import EffectSpec
from ai_secretary.ledger import Ledger

try:
    from ai_secretary.authority import AuthorityStore
    from ai_secretary.ledger import LedgerTx
except ImportError:
    AuthorityStore = LedgerTx = None


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(AuthorityStore, "The same-connection authority foundation is missing")
        self.assertIsNotNone(LedgerTx, "The composable ledger repository is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "operations.sqlite"
        self.store = AuthorityStore(self.path)

    def test_source_and_ledger_changes_roll_back_together(self):
        Ledger(self.path).create_task("task", "user", "Collect reviewed source")
        with self.assertRaisesRegex(RuntimeError, "publication rejected"):
            with self.store.transaction() as uow:
                uow.connection.execute("INSERT INTO source_guard(source_id) VALUES ('source')")
                ledger = LedgerTx(uow)
                ledger.prepare("original-op", EffectSpec("task", "click", {}, {}))
                attempt = ledger.claim("original-op")["attempt_id"]
                uow.connection.execute("UPDATE execution_lifecycle SET epoch=1,enabled=1,status='Running'")
                uow.connection.execute("""INSERT INTO execution_contexts VALUES
                    ('run','session','admission','task','input','scheduler',1,1,NULL,'Active')""")
                uow.connection.execute("INSERT INTO execution_operations VALUES (?,?,?)",
                                       ("original-op", "run", attempt))
                ledger.create_draft("task", "draft", {"body": "Reviewed"})
                ledger.acquire_lease("run", boot_id="boot", now=100, ttl=30)
                raise RuntimeError("publication rejected")
        with self.store.read() as db:
            for table in ("source_guard", "operations", "drafts", "desktop_lease",
                          "execution_contexts", "execution_operations"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT fence FROM lease_counter").fetchone()[0], 0)

    def test_composed_operation_retains_identity_and_cached_result(self):
        with self.store.transaction() as uow:
            ledger = LedgerTx(uow)
            ledger.create_task("task", "user", "Collect reviewed source")
            ledger.prepare("original-op", EffectSpec("task", "click", {}, {}))
            attempt = ledger.claim("original-op")["attempt_id"]
            ledger.finish("original-op", attempt, "Applied", {"artifact": "original"})
        self.assertEqual(Ledger(self.path).claim("original-op"),
                         {"state": "cached", "result": {"artifact": "original"}})

    def test_unit_and_connection_are_invalid_after_commit_and_rollback(self):
        for abort in (False, True):
            with self.subTest(abort=abort):
                try:
                    with self.store.transaction() as uow:
                        connection = uow.connection
                        ledger = LedgerTx(uow)
                        if abort:
                            raise RuntimeError("abort")
                except RuntimeError:
                    pass
                with self.assertRaises(RuntimeError):
                    _ = uow.connection
                with self.assertRaises(RuntimeError):
                    ledger.claim("original-op")
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_nested_transaction_on_same_database_is_refused(self):
        other = AuthorityStore(self.path)
        ledger = Ledger(self.path)
        with self.store.transaction() as uow:
            for enter in (self.store.transaction, other.transaction):
                with self.assertRaisesRegex(RuntimeError, "nested"):
                    with enter():
                        self.fail("Nested transaction entered")
            with self.assertRaisesRegex(RuntimeError, "nested"):
                ledger.create_task("bad", "user", "Must join explicit unit")
            LedgerTx(uow).create_task("good", "user", "Composed")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT task_id FROM tasks").fetchone()[0], "good")

    def test_reads_are_query_only_and_closed_on_exit(self):
        with self.store.read() as db:
            self.assertFalse(db.in_transaction)
            self.assertEqual(db.execute("SELECT lifecycle_id FROM execution_lifecycle").fetchone()[0],
                             "scheduler")
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO tasks VALUES ('bad','user','{}')")
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("CREATE TABLE forbidden(value)")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_new_database_is_private_and_memory_paths_are_refused(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            AuthorityStore(":memory:")

    def test_lease_clock_and_boot_are_sampled_before_unit_entry(self):
        ledger = Ledger(self.path)
        original = self.store.transaction
        entered = False

        def sample_boot():
            self.assertFalse(entered, "Boot discovery must precede BEGIN")
            return "sampled-boot"

        def sample_clock():
            self.assertFalse(entered, "Clock sampling must precede BEGIN")
            return 100.0

        from contextlib import contextmanager

        @contextmanager
        def enter_unit():
            nonlocal entered
            with original() as uow:
                entered = True
                try:
                    yield uow
                finally:
                    entered = False

        with patch.object(ledger.store, "transaction", enter_unit), \
                patch("ai_secretary.ledger._boot_id", sample_boot), \
                patch("ai_secretary.ledger.time.monotonic", sample_clock):
            fence = ledger.acquire_lease("run", ttl=30)
            self.assertTrue(ledger.assert_lease("run", fence))
            ledger.release_lease("run", fence)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(AuthorityStore, "The additive authority migration is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "operations.sqlite"

    def test_existing_operation_draft_and_lease_records_survive_migration(self):
        # This is the deployed pre-foundation schema, not the new migration's SQL.
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE tasks(task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, intent_json TEXT NOT NULL);
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    owner TEXT NOT NULL, effect_json TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                    attempt_id TEXT, result_json TEXT);
                CREATE TABLE drafts(draft_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    content_json TEXT NOT NULL, content_digest TEXT NOT NULL, approved_digest TEXT, approved_actor TEXT);
                CREATE TABLE lease_counter(singleton INTEGER PRIMARY KEY, fence INTEGER NOT NULL);
                CREATE TABLE desktop_lease(singleton INTEGER PRIMARY KEY, owner TEXT NOT NULL, fence INTEGER NOT NULL,
                    boot_id TEXT NOT NULL, expires REAL NOT NULL);
                INSERT INTO tasks VALUES ('original-task','user',' {"intent":"original"} ');
                INSERT INTO operations VALUES ('original-op','original-task','user',' {"effect": 1} ',
                    'original-fingerprint','Applied','original-attempt',' {"artifact": "original"} ');
                INSERT INTO drafts VALUES ('original-draft','original-task',' {"body":"original"} ',
                    'original-digest','original-digest','user');
                INSERT INTO lease_counter VALUES (1,41);
                INSERT INTO desktop_lease VALUES (1,'original-owner',41,'original-boot',500);
            """)
            tables = ("tasks", "operations", "drafts", "lease_counter", "desktop_lease")
            before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        store = AuthorityStore(self.path)
        AuthorityStore(self.path)
        with store.read() as db:
            after = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                     for table in tables}
            self.assertEqual(after, before)
            self.assertEqual(db.execute("SELECT version FROM authority_schema_version").fetchall()[0][0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM authority_schema_version").fetchone()[0], 1)
            self.assertEqual(tuple(db.execute("SELECT * FROM execution_lifecycle").fetchone()),
                             ("scheduler", 0, 0, "Stopped"))
        self.assertEqual(Ledger(self.path).claim("original-op"),
                         {"state": "cached", "result": {"artifact": "original"}})

    def test_migration_failure_rolls_back_every_foundation_statement(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE sentinel(value TEXT)")
            db.execute("INSERT INTO sentinel VALUES ('original')")
            # A legacy name collision fails the final version publication, after
            # earlier CREATEs have run; this exercises actual transactional DDL.
            db.execute("CREATE VIEW authority_schema_version AS SELECT value AS version FROM sentinel")
            db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            AuthorityStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(names, {"sentinel"})
            self.assertEqual(db.execute("SELECT value FROM sentinel").fetchone()[0], "original")
            db.execute("DROP VIEW authority_schema_version")
            db.commit()
        with AuthorityStore(self.path).read() as db:
            self.assertEqual(db.execute("SELECT version FROM authority_schema_version").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
