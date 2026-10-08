"""User-operated local control plane; never registered as a model tool."""
import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from .service import state_dir, handle
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
        result = asyncio.run(handle({"tool": args.tool, "args": json.loads(args.args_json), "session": args.session}))
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
        from .native import NativeClient
        binary = os.environ.get("SECRETARY_NATIVE_BINARY", str(Path(__file__).resolve().parents[2] / "build/desktop-secretary"))
        result = asyncio.run(NativeClient(binary, state_dir=state_dir()).call("status"))
        result["model_endpoint"] = "http://127.0.0.1:11434 (configure an on-prem Ollama model before agent use)"
    else:
        from .rpc import main as rpc_main
        os.environ["SECRETARY_SESSION"] = args.session
        return rpc_main()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def ledger_connection(path):
    import sqlite3
    from contextlib import closing
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row
    return closing(db)

if __name__ == "__main__": main()
