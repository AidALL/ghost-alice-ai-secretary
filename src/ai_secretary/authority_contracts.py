"""Immutable local authority snapshots; constructing one never grants authority."""

from dataclasses import dataclass
import hashlib
import json
import math

from .contracts import canonical_json, identifier


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _finite(value, label, *, nonnegative=False):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and (not nonnegative or value >= 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite{' nonnegative' if nonnegative else ''} number")


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    revision: int
    kind: str
    scope_json: str

    def __post_init__(self):
        identifier(self.source_id, "source_id")
        _positive(self.revision, "revision")
        if self.kind not in ("local_files", "desktop_attachments"):
            raise ValueError("unsupported source kind")
        if type(self.scope_json) is not str:
            raise TypeError("scope_json must be a JSON string")
        scope = json.loads(self.scope_json)
        if type(scope) is not dict:
            raise ValueError("source scope must be a JSON object")
        object.__setattr__(self, "scope_json", canonical_json(scope))

    @classmethod
    def create(cls, source_id, revision, kind, scope):
        return cls(source_id, revision, kind, canonical_json(scope))

    @property
    def scope(self):
        return json.loads(self.scope_json)

    @property
    def scope_digest(self):
        return hashlib.sha256(self.scope_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JobGrant:
    grant_id: str
    source_id: str
    source_revision: int
    session: str
    admission_id: str
    task_id: str
    input_event_id: str
    scope_digest: str
    recipe: str
    expires_at: float

    def __post_init__(self):
        for name in ("grant_id", "source_id", "session", "admission_id", "task_id", "input_event_id", "recipe"):
            identifier(getattr(self, name), name)
        _positive(self.source_revision, "source_revision")
        if (type(self.scope_digest) is not str or len(self.scope_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.scope_digest)):
            raise ValueError("scope_digest must be a canonical SHA-256 digest")
        _finite(self.expires_at, "expires_at")


@dataclass(frozen=True)
class ExecutionContext:
    """Server-derived identity for foreground work without collection configuration."""

    run_id: str
    session: str
    admission_id: str
    task_id: str
    input_event_id: str
    lifecycle_id: str
    epoch: int
    run_fence: int
    desktop_fence: int | None = None

    def __post_init__(self):
        for name in ("run_id", "session", "admission_id", "task_id", "input_event_id", "lifecycle_id"):
            identifier(getattr(self, name), name)
        _positive(self.epoch, "epoch")
        _positive(self.run_fence, "run_fence")
        if self.desktop_fence is not None:
            _positive(self.desktop_fence, "desktop_fence")


@dataclass(frozen=True, kw_only=True)
class RunContext(ExecutionContext):
    """Collection identity adds an immutable grant, source and optional occurrence."""

    grant_id: str
    source_id: str
    source_revision: int
    job_id: str | None = None
    job_revision: int | None = None
    occurrence_id: str | None = None

    def __post_init__(self):
        super().__post_init__()
        identifier(self.grant_id, "grant_id")
        identifier(self.source_id, "source_id")
        _positive(self.source_revision, "source_revision")
        job = (self.job_id, self.job_revision, self.occurrence_id)
        if any(value is not None for value in job):
            if any(value is None for value in job):
                raise ValueError("job identity requires job_id, job_revision and occurrence_id together")
            identifier(self.job_id, "job_id")
            _positive(self.job_revision, "job_revision")
            identifier(self.occurrence_id, "occurrence_id")


@dataclass(frozen=True)
class GateEvidence:
    session: str
    admission_id: str
    task_id: str
    input_event_id: str
    checked_monotonic: float

    def __post_init__(self):
        for name in ("session", "admission_id", "task_id", "input_event_id"):
            identifier(getattr(self, name), name)
        _finite(self.checked_monotonic, "checked_monotonic", nonnegative=True)
