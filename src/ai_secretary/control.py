"""User-operated local control plane; never registered as a model tool."""
import argparse
import asyncio
import json
import os
import sys
import uuid
import time
import signal
from dataclasses import asdict
from pathlib import Path
from .service import state_dir, handle, build_services, build_scheduled_services, stop_scheduler, stop_all
from .ledger import Ledger
from .runtime import run_user_request


def main():
    parser = argparse.ArgumentParser(description="Local desktop secretary control")
    parser.add_argument("--session", default="agent:main:secretary")
    sub = parser.add_subparsers(dest="command", required=True)
    task = sub.add_parser("task"); task.add_argument("text")
    run = sub.add_parser("run"); run.add_argument("text")
    run.add_argument("--runtime", choices=("openclaw",), default=os.environ.get("SECRETARY_RUNTIME", "openclaw"), help="inference-host adapter (OpenClaw is currently shipped)")
    executable = run.add_mutually_exclusive_group()
    executable.add_argument("--runtime-executable", type=Path, help="pinned executable for the selected runtime")
    executable.add_argument("--openclaw", type=Path, help="compatibility alias for --runtime-executable")
    run.add_argument("--new-task", action="store_true", help="start a separate native conversation/task")
    call = sub.add_parser("invoke"); call.add_argument("tool"); call.add_argument("--args-json", default="{}")
    approve = sub.add_parser("approve"); approve.add_argument("draft_id"); approve.add_argument("--digest", required=True)
    sub.add_parser("doctor")
    sub.add_parser("mcp")
    sub.add_parser("stop")
    backend = sub.add_parser("backend"); backend.add_argument("select", choices=("builtin", "peekaboo"))
    backend.add_argument("--binary", type=Path)
    source = sub.add_parser("source"); ss = source.add_subparsers(dest="source_command", required=True)
    configure = ss.add_parser("configure"); configure.add_argument("source_id"); configure.add_argument("--kind", choices=("local_files", "desktop_attachments"), required=True); configure.add_argument("--scope-file", type=Path, required=True); configure.add_argument("--revision", type=int, default=1)
    grant = ss.add_parser("grant"); grant.add_argument("source_id"); grant.add_argument("--expires-at", required=True); grant.add_argument("--text", help="actual operator instruction for a dedicated standing session; omit to bind the current admitted request")
    ss.add_parser("list"); revoke = ss.add_parser("revoke"); revoke.add_argument("source_id")
    collect = sub.add_parser("collect"); collect.add_argument("grant_id"); collect.add_argument("--run-id")
    ingest = sub.add_parser("ingest"); ingest.add_argument("run_id"); ingest.add_argument("retrieval_id"); ingest.add_argument("path", type=Path)
    artifacts = sub.add_parser("artifacts"); artifacts.add_argument("--source-id")
    schedule = sub.add_parser("schedule"); js = schedule.add_subparsers(dest="schedule_command", required=True)
    create = js.add_parser("create"); create.add_argument("grant_id"); when = create.add_mutually_exclusive_group(required=True); when.add_argument("--at"); when.add_argument("--every", type=float); create.add_argument("--start-at"); create.add_argument("--misfire", choices=("skip", "coalesce"), default="skip"); create.add_argument("--max-runs", type=int, default=100); create.add_argument("--max-lateness", type=float, default=60)
    js.add_parser("list"); cancel = js.add_parser("cancel"); cancel.add_argument("job_id")
    js.add_parser("reconcile"); js.add_parser("start"); js.add_parser("stop")
    args = parser.parse_args()
    if args.command == "run":
        from .adapters.openclaw import OpenClawRuntime
        if args.runtime != "openclaw": parser.error("unknown runtime adapter")
        try:
            runtime = OpenClawRuntime(args.runtime_executable or args.openclaw)
        except ValueError as error:
            parser.error(str(error))
        try:
            code = asyncio.run(run_user_request(args.text, runtime=runtime, route=args.session, new_task=args.new_task))
        except KeyboardInterrupt:
            raise SystemExit(130)
        if code: raise SystemExit(code)
        return
    if args.command == "task":
        result = asyncio.run(handle({"kind": "intake", "session": args.session, "admission_id": str(uuid.uuid4()), "text": args.text, "origin": "external_user", "new_task": True}))
    elif args.command == "invoke":
        result = asyncio.run(build_services().call(args.tool, json.loads(args.args_json), args.session))
    elif args.command == "approve":
        if not sys.stdin.isatty(): parser.error("approval requires the user's interactive terminal")
        ledger = Ledger(state_dir() / "operations.sqlite")
        # Print the stored draft, never a second model-supplied preview.
        with ledger_connection(state_dir() / "operations.sqlite") as db:
            row = db.execute("SELECT * FROM drafts WHERE draft_id=?", (args.draft_id,)).fetchone()
            if row is None: parser.error("unknown draft")
            print(json.dumps(dict(row), ensure_ascii=False, indent=2))
        confirmation = input(f"발신 내용을 확인했다면 APPROVE {args.digest} 를 입력하세요: ")
        if confirmation != f"APPROVE {args.digest}": parser.error("approval not granted")
        ledger.approve(args.draft_id, args.digest, "user:" + str(os.getuid()))
        result = {"draft_id": args.draft_id, "approved_digest": args.digest}
    elif args.command == "doctor":
        result = asyncio.run(build_services().native.call("status"))
        result["model_endpoint"] = "http://127.0.0.1:11434 (configure an on-prem Ollama model before agent use)"
    elif args.command in {"source", "collect", "ingest", "artifacts", "schedule", "backend", "stop"}:
        result = asyncio.run(operator_command(args))
    else:
        from .rpc import main as rpc_main
        os.environ["SECRETARY_SESSION"] = args.session
        return rpc_main()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def instant(value):
    from datetime import datetime
    try: return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None: raise ValueError("timestamp requires a timezone")
        return parsed.timestamp()


async def operator_command(args):
    from .authority_contracts import SourceSpec
    from .coordinator import LifecycleTx
    from .governance import Governance
    from .operator import Operator
    root = state_dir().resolve()
    if args.command == "backend":
        config_file = root / "runtime.json"
        config = json.loads(config_file.read_text()) if config_file.exists() else {}
        binary = (args.binary or Path(__file__).resolve().parents[2] / "build" / ("desktop-secretary-peekaboo" if args.select == "peekaboo" else "desktop-secretary")).resolve()
        if not binary.is_file(): raise ValueError("selected native binary must exist")
        from .native import NativeClient
        status = await NativeClient(binary, state_dir=root, backend=args.select).call("status")
        if status.get("ok") is not True: raise ValueError("selected native backend is unavailable")
        config.update(native_backend=args.select, native_binary=str(binary))
        from .contracts import canonical_json
        temporary = root / (".runtime-" + str(uuid.uuid4()))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical_json(config)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, config_file)
        return {"selected_backend": args.select, "native_binary": str(binary), "status": status}
    if args.command == "schedule":
        from .jobs import JobRepository, OutboxReconciler, ScheduleSpec
        store, manager, _, _ = build_scheduled_services(root)
        if args.schedule_command == "list":
            with store._read_unit() as unit: return {"jobs": [asdict(job) for job in JobRepository(unit).jobs()]}
        if args.schedule_command == "create":
            schedule = ScheduleSpec("at", instant(args.at), misfire=args.misfire) if args.at else ScheduleSpec("every", args.every, start_at=instant(args.start_at) if args.start_at else None, misfire=args.misfire)
            return asdict(await manager.create(args.grant_id, schedule, max_runs=args.max_runs, max_lateness=args.max_lateness))
        if args.schedule_command == "cancel":
            await manager.cancel(args.job_id); return {"job_id": args.job_id, "cancelled": True}
        if args.schedule_command == "stop": return await stop_scheduler(manager)
        reconciler = OutboxReconciler(manager)
        if args.schedule_command == "reconcile":
            await reconciler.tick(); return {"reconciled": True}
        with store.transaction() as unit: LifecycleTx(unit).start("scheduler")
        stop = asyncio.Event(); loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(signum, stop.set)
        try: await reconciler.run(stop)
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM): loop.remove_signal_handler(signum)
        return await stop_scheduler(manager)
    secretary = build_services(root)
    if args.command == "stop":
        config_file = root / "runtime.json"
        config = json.loads(config_file.read_text()) if config_file.exists() else {}
        manager = build_scheduled_services(root)[1] if all(name in config for name in ("openclaw_executable", "openclaw_state", "openclaw_config")) else None
        return await stop_all(secretary, scheduler=manager)
    operator = Operator(secretary, Governance(root))
    if args.command == "source":
        if args.source_command == "configure": return asdict(operator.configure(SourceSpec.create(args.source_id, args.revision, args.kind, json.loads(args.scope_file.read_text()))))
        if args.source_command == "grant": return asdict(await operator.grant(args.source_id, args.session, instant(args.expires_at), text=args.text))
        if args.source_command == "list": return {"sources": operator.sources()}
        operator.revoke(args.source_id); return {"source_id": args.source_id, "revoked": True}
    if args.command == "collect": return (await operator.collect(args.grant_id, run_id=args.run_id)).as_dict()
    if args.command == "ingest": return (await operator.ingest(args.run_id, args.retrieval_id, str(args.path.resolve()))).as_dict()
    with secretary.ledger.store.read() as db:
        sql = "SELECT * FROM collection_artifacts" + (" WHERE source_id=?" if args.source_id else "") + " ORDER BY created_at DESC LIMIT 1000"
        return {"artifacts": [dict(row) for row in db.execute(sql, (args.source_id,) if args.source_id else ())]}


def ledger_connection(path):
    import sqlite3
    from contextlib import closing
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row
    return closing(db)

if __name__ == "__main__": main()
