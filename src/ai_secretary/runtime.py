"""Admission and conversation ownership independent of the inference host."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Protocol
import uuid

from .governance import Governance
from .policy import require_on_prem_provider


@dataclass(frozen=True)
class RunRequest:
    text: str
    route: str
    conversation_id: str
    admission_id: str
    task_id: str
    input_event_id: str
    new_task: bool

    @property
    def binding(self):
        return self.route + "::" + self.conversation_id


class Runtime(Protocol):
    """An adapter declares its provider route and dispatches admitted requests."""
    provider: str

    async def dispatch(self, request: RunRequest) -> int: ...


async def run_user_request(text: str, *, runtime: Runtime, route: str = "agent:main:secretary",
                           new_task: bool = False, root: Path | None = None) -> int:
    require_on_prem_provider(runtime.provider)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("actual user text required")
    if not isinstance(route, str) or not route.strip():
        raise ValueError("conversation route required")
    if root is None:
        from .service import state_dir
        root = state_dir()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep the persisted layout/identity used by earlier user-run admissions.
    with closing(sqlite3.connect(root / "control-sessions.sqlite", timeout=10)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations(route TEXT PRIMARY KEY,native_session TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT native_session FROM conversations WHERE route=?", (route,)).fetchone()
            conversation = str(uuid.uuid4()) if not row or new_task else row[0]
            db.execute("INSERT OR REPLACE INTO conversations VALUES(?,?)", (route, conversation))
    binding = route + "::" + conversation
    admission_id = str(uuid.uuid4())
    governance = Governance(root)
    admission = governance.intake(binding, admission_id, text, "external_user", new_task=new_task)
    gate = await governance.check(binding)
    if (gate.get("decision") != "allow" or gate.get("task_id") != admission["task_id"]
            or gate.get("inputEventId") != admission["inputEventId"]):
        raise PermissionError("Ghost-ALICE current gate blocks runtime dispatch")
    request = RunRequest(text, route, conversation, admission_id, admission["task_id"],
                         admission["inputEventId"], new_task)
    return await runtime.dispatch(request)
