"""Bridge real native intake and the installed Ghost-ALICE decision core."""
import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
from contextlib import contextmanager
from importlib.resources import as_file
from pathlib import Path
from .resource_loader import resource

# Existing ledger identities and admitted conversations use this namespace.
# It is compatibility storage, independent of the selected inference adapter.
LEGACY_LEDGER_PLATFORM = "openclaw"


class Governance:
    def __init__(self, root):
        self.root = Path(root)
        self.ghost = Path(os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")))
        self.ledger_root = Path(os.environ.get("SECRETARY_GA_LEDGER_ROOT", str(self.root / "governance")))
        self.db = self.root / "admissions.sqlite"
        self.platform = LEGACY_LEDGER_PLATFORM
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS admission(session TEXT PRIMARY KEY, admission_id TEXT, digest TEXT, event_id TEXT, task_id TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS admission_history(session TEXT, admission_id TEXT, digest TEXT, PRIMARY KEY(session,admission_id))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        try:
            with db: yield db
        finally: db.close()
    @staticmethod
    def identity(session): return "secretary-" + hashlib.sha256(session.encode()).hexdigest()[:40]

    def intake(self, session, admission_id, text, origin, *, new_task=False):
        if origin != "external_user": raise PermissionError("intake needs classified actual user input")
        if not isinstance(text, str) or not text.strip() or not admission_id: raise ValueError("actual user text/admission identity required")
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT admission_id,digest,event_id,task_id FROM admission WHERE session=?", (session,)).fetchone()
            if row and row[0] == admission_id:
                if row[1] != digest: raise ValueError("admission identity reused for different text")
                return {"inputEventId": row[2], "task_id": row[3]}
            if db.execute("SELECT 1 FROM admission_history WHERE session=? AND admission_id=?", (session, admission_id)).fetchone():
                raise PermissionError("old admission cannot replace the current task")
            source = self.ghost / "session-intent-analyzer/scripts/session_intent_ledger.py"
            spec = importlib.util.spec_from_file_location("secretary_ga_intake", source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            native_session = self.identity(session)
            observation = module.build_input_observation(platform=self.platform, session_id=native_session, raw_user_input=text)
            module.record_turn(root=self.ledger_root, platform=self.platform, session_id=native_session, raw_user_input=text, observation=observation, source="ai-secretary-native-admission")
            event = observation["input_event_id"]
            task = row[3] if row and not new_task else "task-" + hashlib.sha256((session + event).encode()).hexdigest()[:40]
            from .ledger import Ledger
            ledger = Ledger(self.root / "operations.sqlite")
            from .ledger import LedgerTx
            with ledger.store.transaction() as unit:
                if not row or new_task:
                    LedgerTx(unit).create_task(task, "user", text)
                # The native acceptance point must see a new local admission
                # even if it races after the async Ghost check. This is local
                # invalidation, not a global transaction with Ghost storage.
                from .coordinator import AuthorityRepository
                AuthorityRepository.initialize(unit)
                AuthorityRepository(unit).record_admission(session, admission_id, task, event)
            db.execute("INSERT OR REPLACE INTO admission VALUES(?,?,?,?,?)", (session, admission_id, digest, event, task))
            db.execute("INSERT INTO admission_history VALUES(?,?,?)", (session, admission_id, digest))
            return {"inputEventId": event, "task_id": task}

    async def check(self, session):
        with self.connect() as db:
            row = db.execute("SELECT event_id,task_id,admission_id FROM admission WHERE session=?", (session,)).fetchone()
        if not row: raise PermissionError("no actual user intake bound to this conversation")
        with as_file(resource("ga-gate.mjs")) as script:
            child = await asyncio.create_subprocess_exec(os.environ.get("SECRETARY_NODE", "node"), str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            envelope = {"ghostRoot": str(self.ghost), "root": str(self.ledger_root), "platform": self.platform, "sessionId": self.identity(session), "inputEventId": row[0]}
            try:
                output, error = await asyncio.wait_for(child.communicate(json.dumps(envelope).encode()), 5)
            except BaseException:
                if child.returncode is None: child.kill()
                await child.wait()
                raise
        if child.returncode or len(output) > 65536: raise PermissionError("Ghost-ALICE bridge unavailable")
        result = json.loads(output)
        if result.get("inputEventId") != row[0]: raise PermissionError("stale Ghost-ALICE input lineage")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT event_id,task_id,admission_id FROM admission WHERE session=?", (session,)).fetchone()
            if current != row:
                raise PermissionError("native admission changed during Ghost-ALICE check")
            # Upgrade the current legacy admission without manufacturing intake.
            # Hold its short local writer lock so an older sample cannot overwrite
            # a concurrent genuine admission's authority mirror.
            from .authority import AuthorityStore
            from .coordinator import AuthorityRepository
            store = AuthorityStore(self.root / "operations.sqlite")
            with store.transaction() as unit:
                AuthorityRepository.initialize(unit)
                AuthorityRepository(unit).record_admission(session, row[2], row[1], row[0])
        return {**result, "task_id": row[1], "admission_id": row[2]}
