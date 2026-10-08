"""Local secretary tool executor. Dependencies: Python standard library, native helper."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from .contracts import EffectSpec, ApprovalRequired
from .ledger import Ledger

TOOL_NAMES = ("secretary_status", "secretary_apps", "secretary_windows", "secretary_observe", "secretary_act", "secretary_files", "secretary_draft")
READ_COMMANDS = {"secretary_status": "status", "secretary_apps": "apps", "secretary_windows": "windows", "secretary_observe": "observe"}
ACTIONS = {"focus", "click", "drag", "scroll", "type", "hotkey", "open"}


class Secretary:
    def __init__(self, ledger, native, gate):
        self.ledger, self.native, self.gate = ledger, native, gate

    async def call(self, tool, args, session):
        if tool not in TOOL_NAMES: raise ValueError("unknown secretary tool")
        binding = await self.gate(session)
        if binding.get("decision") != "allow": raise PermissionError("Ghost-ALICE current gate blocks execution")
        task_id = binding["task_id"]
        if tool in READ_COMMANDS:
            return await self.native.call(READ_COMMANDS[tool], **args)
        if tool == "secretary_files":
            path = Path(args["path"]).expanduser().resolve()
            if not path.is_dir(): raise ValueError("path must be an existing directory")
            return {"path": str(path), "entries": [{"name": p.name, "directory": p.is_dir(), "size": p.stat().st_size} for p in sorted(path.iterdir())][:1000]}
        if tool == "secretary_draft":
            content = args["content"]
            if not isinstance(content, dict) or not all(k in content for k in ("account", "recipients", "body", "attachments")):
                raise ValueError("draft requires account, recipients, body and attachment hashes")
            digest = self.ledger.create_draft(task_id, args["draft_id"], content)
            return {"draft_id": args["draft_id"], "digest": digest, "content": content, "approval": "user_required"}
        params = dict(args)
        operation = params.pop("operation_id")
        action = params.pop("action")
        if action not in ACTIONS: raise ValueError("unknown native action")
        outbound = params.pop("outbound", None)
        if outbound:
            self.ledger.validate_approval(outbound["draft_id"], outbound["content"], task_id=task_id)
        # This is an assistive semantic guard. Arbitrary apps need the skill's visual
        # interpretation too; a label heuristic cannot certify every possible send UI.
        label = str(params.pop("target_label", "")).strip().casefold()
        if label in {"send", "send message", "send invitation", "전송", "보내기", "发送", "發送", "초대"} and not outbound:
            raise ApprovalRequired("outgoing content needs the user's exact draft approval")
        expected = params.get("expected", {})
        target = {k: expected.get(k) for k in ("pid", "process_start", "window_id")}
        # Short-lived observation tokens and timestamps are guard material, not
        # business semantics. A refreshed observation can reconcile the same intent.
        semantic = {k: v for k, v in params.items() if k != "expected"}
        semantic["outbound"] = outbound
        self.ledger.prepare(operation, EffectSpec(task_id, action, target, semantic))
        lease_owner = str(uuid.uuid4())
        fence = self.ledger.acquire_lease(lease_owner, ttl=30)
        attempt = None
        try:
            self.ledger.assert_lease(lease_owner, fence)
            current = await self.gate(session)
            if (current.get("decision") != "allow" or current.get("task_id") != task_id
                    or current.get("inputEventId") != binding.get("inputEventId")):
                raise PermissionError("current task or gate changed before effect")
            if outbound: self.ledger.validate_approval(outbound["draft_id"], outbound["content"], task_id=task_id)
            claim = self.ledger.claim(operation)
            if claim["state"] != "claimed": return claim
            attempt = claim["attempt_id"]
            self.ledger.assert_lease(lease_owner, fence)
            result = await self.native.call(action, **params)
            if result.get("ok") is False:
                state = "Failed" if result.get("effect_status") == "not_started" else "Unknown"
                result = {**result, "no_effect": state == "Failed"}
            else: state = "Applied"
            saved = self.ledger.finish(operation, attempt, state, result)
            return {"state": saved.get("status", saved.get("state", state)), "operation_id": operation, "result": result}
        except asyncio.CancelledError:
            if attempt: self.ledger.finish(operation, attempt, "Unknown", {"error": "cancelled; effect requires reconciliation"})
            raise
        except Exception as error:
            if not attempt: raise
            no_effect = hasattr(error, "effect_uncertain") and not error.effect_uncertain
            state = "Failed" if no_effect else "Unknown"
            self.ledger.finish(operation, attempt, state, {"error": str(error), "no_effect": no_effect})
            return {"state": state, "operation_id": operation, "error": str(error)}
        finally:
            self.ledger.release_lease(lease_owner, fence)


def state_dir():
    root = Path(os.environ.get("SECRETARY_STATE_DIR", str(Path.home() / "Library/Application Support/AI_Secretary")))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


async def handle(request):
    from .governance import Governance
    from .native import NativeClient
    root = state_dir()
    governance = Governance(root)
    kind = request.get("kind", "call")
    if kind == "intake":
        return governance.intake(request["session"], request["admission_id"], request["text"], request.get("origin", "unknown"), new_task=request.get("new_task") is True)
    if kind == "gate": return await governance.check(request["session"])
    native = NativeClient(os.environ.get("SECRETARY_NATIVE_BINARY", str(Path(__file__).resolve().parents[2] / "build/desktop-secretary")), state_dir=root)
    ledger = Ledger(root / "operations.sqlite")
    return await Secretary(ledger, native, governance.check).call(request["tool"], request.get("args", {}), request["session"])


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
