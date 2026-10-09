"""Local secretary tool executor. Dependencies: Python standard library, native helper."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from .contracts import ApprovalRequired
from .coordinator import AuthorityDenied, AuthorityRepository, RunCoordinator
from .execution import DesktopExecutor, make_effect_spec
from .ledger import Ledger, LedgerTx
from .worker_sessions import WorkerSession, WorkerSessions, validate_worker_call
import time
from dataclasses import asdict

TOOL_NAMES = ("secretary_status", "secretary_apps", "secretary_windows", "secretary_observe", "secretary_act", "secretary_files", "secretary_draft", "secretary_collect", "secretary_ingest", "secretary_artifacts")
READ_COMMANDS = {"secretary_status": "status", "secretary_apps": "apps", "secretary_windows": "windows", "secretary_observe": "observe"}
ACTIONS = {"focus", "click", "drag", "scroll", "type", "hotkey", "open"}


class Secretary:
    def __init__(self, ledger, native, gate, *, coordinator=None, collection=None):
        self.ledger, self.native, self.gate = ledger, native, gate
        self.coordinator = coordinator or RunCoordinator(ledger.store, self._current_gate)
        self.executor = DesktopExecutor(ledger.store, self.coordinator, native)
        self.collection = collection

    async def _current_gate(self, session):
        return await self.gate(session)

    async def call(self, tool, args, session):
        # Trusted local operator convenience. Model transports use a host-owned
        # persistent capability and call_bound, never caller-selected identity.
        args = validate_worker_call(tool, args, allowed_tools=TOOL_NAMES)
        registry = WorkerSessions(self.ledger.store, self.coordinator)
        transport = "operator:" + str(uuid.uuid4())
        binding = await registry.open(session, transport)
        try:
            result = await WorkerSession(self, binding, allowed_tools=TOOL_NAMES).call(tool, args)
        finally:
            cleanup = await registry.close(binding.token, transport, session)
        return {**result, "worker_cleanup": asdict(cleanup)}

    async def call_bound(self, tool, args, context):
        args = validate_worker_call(tool, args, allowed_tools=TOOL_NAMES)
        await self.coordinator.checkpoint(context, "worker-read")
        task_id = context.task_id
        if tool in READ_COMMANDS:
            return await self.native.call(READ_COMMANDS[tool], **args)
        if tool == "secretary_files":
            import itertools
            path = Path(args["path"]).expanduser().resolve()
            if not path.is_dir(): raise ValueError("path must be an existing directory")
            entries = list(itertools.islice(path.iterdir(), 1001))
            return {"path": str(path), "truncated": len(entries) > 1000, "entries": [{"name": p.name, "directory": p.is_dir(), "size": p.lstat().st_size} for p in sorted(entries[:1000])]}
        if tool == "secretary_draft":
            content = args["content"]
            if not isinstance(content, dict) or not all(k in content for k in ("account", "recipients", "body", "attachments", "composer", "intent")):
                raise ValueError("draft requires account, recipients, body, attachment hashes, composer and intent")
            composer = content["composer"]
            if (not isinstance(composer, dict) or not all(k in composer for k in ("pid", "process_start", "window_id"))
                    or type(content["body"]) is not str or not isinstance(content["recipients"], list)
                    or not isinstance(content["attachments"], list) or not content["account"] or not content["intent"]):
                raise ValueError("draft must bind an exact composer and outgoing content")
            for attachment in content["attachments"]:
                digest = attachment.get("sha256") if isinstance(attachment, dict) else None
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ValueError("each attachment requires its exact SHA-256")
            now = time.time()
            with self.ledger.store.transaction() as unit:
                self.coordinator.validate_local(unit, context, "draft", now=now)
                digest = LedgerTx(unit).create_draft(task_id, args["draft_id"], content)
            return {"draft_id": args["draft_id"], "digest": digest, "content": content, "approval": "user_required"}
        if tool == "secretary_artifacts":
            from .collection import CollectionRepository
            with self.ledger.store._read_unit() as unit:
                rows = unit.connection.execute("SELECT * FROM collection_artifacts" + (" WHERE source_id=?" if args.get("source_id") else "") + " ORDER BY created_at DESC LIMIT 1000", (args["source_id"],) if args.get("source_id") else ()).fetchall()
            return {"artifacts": [dict(row) for row in rows]}
        if tool in ("secretary_collect", "secretary_ingest"):
            if self.collection is None:
                raise ValueError("collection service is not configured")
            collection_context = await self._collection_context(context, args["source_id"])
            source = await self.coordinator.source(collection_context)
            if tool == "secretary_collect":
                result = await self.collection.collect(collection_context, source)
            else:
                if self.collection.desktop is None:
                    raise ValueError("desktop collection recipe is not configured")
                candidate = await self.collection.desktop.correlate(collection_context, source, args["retrieval_id"], args["path"])
                result = await self.collection.ingest_run(collection_context, source, args["retrieval_id"], candidate)
            await self.coordinator.finish_collection(collection_context, result.status)
            return result.as_dict()
        params = dict(args)
        operation = params.pop("operation_id")
        action = params.pop("action")
        if action not in ACTIONS: raise ValueError("unknown native action")
        outbound = params.pop("outbound", None)
        if outbound:
            self.ledger.validate_approval(outbound["draft_id"], outbound["content"], task_id=task_id)
            expected = params.get("expected", {})
            composer = outbound["content"].get("composer", {})
            if not composer or any(composer.get(name) != expected.get(name) for name in ("pid", "process_start", "window_id")):
                raise ApprovalRequired("the approved outgoing composer does not match this target")
        # This is an assistive semantic guard. Arbitrary apps need the skill's visual
        # interpretation too; a label heuristic cannot certify every possible send UI.
        label = str(params.pop("target_label", "")).strip().casefold()
        if label in {"send", "send message", "send invitation", "전송", "보내기", "发送", "發送", "초대"} and not outbound:
            raise ApprovalRequired("outgoing content needs the user's exact draft approval")
        spec = make_effect_spec(task_id, action, params, outbound=outbound)
        return await self.executor.dispatch(context, operation, spec, action, params, outbound=outbound)

    async def _collection_context(self, bound, source_id):
        with self.ledger.store._read_unit() as unit:
            guard = unit.connection.execute("SELECT active_run_id FROM source_guard WHERE source_id=?", (source_id,)).fetchone()
            prior = AuthorityRepository(unit).context(guard[0]) if guard and guard[0] else None
        if prior:
            if any(getattr(prior, name) != getattr(bound, name) for name in ("session", "admission_id", "task_id", "input_event_id", "lifecycle_id", "epoch")):
                raise AuthorityDenied("overlap", "source belongs to another run or unresolved retrieval")
            await self.coordinator.checkpoint(prior, "resume-collection")
            return prior
        return await self.coordinator.begin_interactive(bound, source_id)


def state_dir():
    root = Path(os.environ.get("SECRETARY_STATE_DIR", str(Path.home() / "Library/Application Support/AI_Secretary")))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def build_services(root=None):
    from .collection import CollectionService
    from .governance import Governance
    from .native import NativeClient
    root = Path(root or state_dir()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = root / "runtime.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    backend = config.get("native_backend", "builtin")
    binary = config.get("native_binary") or os.environ.get("SECRETARY_NATIVE_BINARY")
    if not binary:
        binary = str(Path(__file__).resolve().parents[2] / "build" / ("desktop-secretary-peekaboo" if backend == "peekaboo" else "desktop-secretary"))
    native = NativeClient(binary, state_dir=root, backend=backend)
    ledger = Ledger(root / "operations.sqlite")
    secretary = Secretary(ledger, native, Governance(root).check)
    from .desktop_collection import DesktopAttachmentRecipe
    desktop = DesktopAttachmentRecipe(ledger.store, secretary.coordinator, native, executor=secretary.executor)
    secretary.collection = CollectionService(ledger.store, root / "artifacts", secretary.coordinator, desktop=desktop)
    return secretary


def build_scheduled_services(root):
    from .adapters.openclaw_scheduler import OpenClawScheduler
    from .jobs import JobManager, RunnerConfig
    root = Path(root).expanduser().resolve()
    secretary = build_services(root)
    config = json.loads((root / "runtime.json").read_text())
    scheduler = OpenClawScheduler(config["openclaw_executable"], config["openclaw_state"], config["openclaw_config"],
                                  isolation_root=str(root.parent))
    manager = JobManager(secretary.ledger.store, secretary.coordinator, scheduler,
                         RunnerConfig(config.get("python_executable", str(Path(sys.executable).resolve())), str(root)))
    return secretary.ledger.store, manager, secretary.coordinator, lambda: secretary.collection


async def stop_scheduler(manager, *, drain_timeout=5):
    from .coordinator import LifecycleTx
    from .desktop_owner import DesktopOwnership
    from .jobs import JobRepository, OutboxReconciler
    # Fence new claims before any upstream cleanup await.
    with manager.store.transaction() as unit:
        epoch = LifecycleTx(unit).disable("scheduler")
        repo = JobRepository(unit)
        for job in repo.jobs():
            repo.cancel(job.job_id)
    await OutboxReconciler(manager)._cleanup()
    try:
        async with DesktopOwnership(manager.store.path, timeout=drain_timeout) as owner:
            with manager.store.transaction() as unit:
                WorkerSessions._account(unit, "scheduler")
                if WorkerSessions._unresolved_projection(unit, "scheduler"):
                    return {"state": "Draining", "epoch": epoch, "disposition": "unresolved_projection"}
                owner.validate()
                lifecycle = LifecycleTx(unit)
                lifecycle.record_drain("scheduler", epoch)
                lifecycle.mark_stopped("scheduler", epoch)
            return {"state": "Stopped", "epoch": epoch, "disposition": "drained"}
    except TimeoutError:
        return {"state": "Draining", "epoch": epoch, "disposition": "timeout"}


async def stop_all(secretary, *, scheduler=None, drain_timeout=5):
    from .coordinator import LifecycleTx
    from .desktop_owner import DesktopOwnership
    store = secretary.ledger.store
    WorkerSessions(store, secretary.coordinator)
    with store.transaction() as unit:
        lifetimes = [row[0] for row in unit.connection.execute("SELECT lifecycle_id FROM execution_lifecycle WHERE status<>'Stopped'")]
        epochs = {name: LifecycleTx(unit).disable(name) for name in lifetimes}
        unit.connection.execute("UPDATE worker_sessions SET state='Closed' WHERE state='Active'")
    if scheduler is not None and "scheduler" in epochs:
        await stop_scheduler(scheduler, drain_timeout=drain_timeout)
    results = []
    try:
        async with DesktopOwnership(store.path, timeout=drain_timeout) as owner:
            with store.transaction() as unit:
                for name, epoch in epochs.items():
                    row = unit.connection.execute("SELECT * FROM execution_lifecycle WHERE lifecycle_id=?", (name,)).fetchone()
                    if row["status"] == "Stopped":
                        results.append({"lifecycle_id": name, "state": "Stopped"}); continue
                    if row["status"] != "Draining" or row["epoch"] != epoch:
                        raise AuthorityDenied("stale", "lifecycle changed during stop")
                    WorkerSessions._account(unit, name)
                    if WorkerSessions._unresolved_projection(unit, name):
                        results.append({"lifecycle_id": name, "state": "Draining", "disposition": "unresolved_projection"}); continue
                    owner.validate()
                    lifecycle = LifecycleTx(unit)
                    lifecycle.record_drain(name, epoch); lifecycle.mark_stopped(name, epoch)
                    results.append({"lifecycle_id": name, "state": "Stopped"})
    except TimeoutError:
        return {"state": "Draining", "disposition": "timeout"}
    return {"state": "Draining" if any(row["state"] == "Draining" for row in results) else "Stopped", "lifecycles": results}


async def handle(request):
    from .governance import Governance
    root = state_dir()
    governance = Governance(root)
    kind = request.get("kind", "call")
    if kind == "intake":
        return governance.intake(request["session"], request["admission_id"], request["text"], request.get("origin", "unknown"), new_task=request.get("new_task") is True)
    if kind == "gate": return await governance.check(request["session"])
    if kind not in {"worker_open", "worker_close", "call"}:
        raise PermissionError("unsupported worker transport operation")
    if kind == "call":
        args = validate_worker_call(request["tool"], request.get("args", {}), allowed_tools=TOOL_NAMES)
        if not request.get("capability") or not request.get("transport_id"):
            raise PermissionError("model calls require a host-private bound worker capability")
    secretary = build_services(root)
    registry = WorkerSessions(secretary.ledger.store, secretary.coordinator)
    if kind == "worker_open":
        return asdict(await registry.open(request["session"], request["transport_id"]))
    if kind == "worker_close":
        return asdict(await registry.close(request["capability"], request["transport_id"], request["session"]))
    context = await registry.resolve(request["capability"], request["transport_id"], request["session"])
    return await secretary.call_bound(request["tool"], args, context)


def main():
    # One request per child gives deterministic ownership/close semantics. All
    # durable state is SQLite; no inherited model process carries approval state.
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    try:
        if len(raw) > 8 * 1024 * 1024: raise ValueError("request too large")
        result = asyncio.run(handle(json.loads(raw)))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
    except Exception as error:
        print(json.dumps({"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__": main()
