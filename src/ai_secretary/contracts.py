"""Immutable, canonical effect bindings shared by worker and user control."""

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any


class BindingConflict(RuntimeError):
    """An identifier was reused with a different task, owner, or final effect."""


class ApprovalRequired(RuntimeError):
    """The exact outgoing draft does not have current user approval."""


class LeaseBusy(RuntimeError):
    """The desktop lease is occupied, expired, or fenced out."""


def _validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite values are not valid JSON")
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            _validate_json(item)
        return
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically without lossy keys or non-finite numbers."""
    _validate_json(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def content_digest(content: Any) -> str:
    """Hash every content field, including account, recipient and attachments."""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def identifier(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


@dataclass(frozen=True, init=False)
class EffectSpec:
    """Snapshot of the final resolved effect; input mutations cannot alter it.

    ``target`` and ``payload`` return fresh JSON copies. Canonical JSON is stored
    internally so nested mutable inputs cannot evade the frozen boundary.
    """

    task_id: str
    kind: str
    _target_json: str = field(repr=False)
    _payload_json: str = field(repr=False)

    def __init__(self, task_id: str, kind: str, target: Any, payload: Any):
        object.__setattr__(self, "task_id", identifier(task_id, "task_id"))
        object.__setattr__(self, "kind", identifier(kind, "kind"))
        object.__setattr__(self, "_target_json", canonical_json(target))
        object.__setattr__(self, "_payload_json", canonical_json(payload))

    @property
    def target(self) -> Any:
        return json.loads(self._target_json)

    @property
    def payload(self) -> Any:
        return json.loads(self._payload_json)

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "kind": self.kind,
                "target": self.target, "payload": self.payload}

    @property
    def canonical(self) -> str:
        return canonical_json(self.as_dict())

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()
