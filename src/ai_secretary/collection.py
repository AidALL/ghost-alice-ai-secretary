"""Bounded stable snapshots and evidence-bound desktop attachment publication.

The repository only uses its owner's active SQLite unit. The asynchronous service
does all gate calls and file IO outside that unit; file publication deliberately
precedes the final authority check, so a rejected check can leave only an orphan
content-addressed blob. Native retrieval and its guarded effect belong to a
server-selected desktop port, never to an arbitrary callback supplied by a model.
"""

from dataclasses import asdict, dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from .authority import UnitOfWork
from .authority_contracts import RunContext, SourceSpec
from .contracts import BindingConflict, canonical_json, identifier


@dataclass(frozen=True)
class SourceItem:
    item_id: str
    provenance_json: str

    def __post_init__(self):
        identifier(self.item_id, "item_id")
        value = json.loads(self.provenance_json)
        if type(value) is not dict:
            raise ValueError("item provenance must be a JSON object")
        object.__setattr__(self, "provenance_json", canonical_json(value))

    @property
    def provenance(self):
        return json.loads(self.provenance_json)


@dataclass(frozen=True)
class FileCandidate(SourceItem):
    # Explicit initializer keeps the public order path, item_id, provenance_json.
    path: Path
    source_complete: bool = False
    correlated: bool = False
    observed_identity_json: str | None = None
    expected_sha256: str | None = None

    def __init__(self, path, item_id, provenance_json, source_complete=False, correlated=False, *, observed_identity_json=None, expected_sha256=None):
        object.__setattr__(self, "path", Path(path))
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "provenance_json", provenance_json)
        object.__setattr__(self, "source_complete", source_complete)
        object.__setattr__(self, "correlated", correlated)
        if observed_identity_json is not None:
            identity = json.loads(observed_identity_json)
            if type(identity) is not dict or set(identity) != {"device", "inode", "size", "mtime_ns", "ctime_ns"} or any(type(v) is not int for v in identity.values()):
                raise ValueError("observed file identity must contain exact integer stat fields")
            observed_identity_json = canonical_json(identity)
        if expected_sha256 is not None and (type(expected_sha256) is not str or len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256)):
            raise ValueError("expected download digest must be canonical SHA-256")
        object.__setattr__(self, "observed_identity_json", observed_identity_json)
        object.__setattr__(self, "expected_sha256", expected_sha256)
        self.__post_init__()
        if type(source_complete) is not bool or type(correlated) is not bool:
            raise TypeError("completion and correlation evidence must be explicit booleans")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    source_id: str
    source_revision: int
    item_id: str
    run_id: str
    retrieval_id: str
    sha256: str
    size: int
    provenance_json: str
    file_metadata_json: str
    created_at: float


@dataclass(frozen=True)
class CollectionResult:
    status: str
    artifacts: tuple[Artifact, ...] = ()
    reason: str = ""

    def as_dict(self):
        return {"status": self.status, "artifacts": [asdict(item) for item in self.artifacts],
                "reason": self.reason}


class CollectionQuotaExceeded(BindingConflict):
    """The immutable run quota cannot admit another verified artifact."""


def _limits(source):
    values = []
    for name, default in (("max_bytes", 64 * 1024 * 1024), ("max_items", 100)):
        value = source.scope.get(name, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        values.append(value)
    return tuple(values)


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS collection_retrieval_metadata (
        retrieval_id TEXT PRIMARY KEY REFERENCES authority_retrievals(retrieval_id),
        source_revision INTEGER NOT NULL, item_provenance_json TEXT NOT NULL,
        context_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collection_no_effect_evidence (
        retrieval_id TEXT PRIMARY KEY REFERENCES authority_retrievals(retrieval_id),
        operation_id TEXT REFERENCES operations(operation_id), evidence_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collection_retrieval_history (
        history_id INTEGER PRIMARY KEY,
        retrieval_id TEXT NOT NULL REFERENCES authority_retrievals(retrieval_id),
        run_id TEXT NOT NULL REFERENCES authority_runs(run_id),
        operation_id TEXT REFERENCES operations(operation_id), source_revision INTEGER NOT NULL,
        context_json TEXT NOT NULL, item_provenance_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state='NoEffect'), evidence_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collection_artifacts (
        artifact_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
        item_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES authority_runs(run_id),
        retrieval_id TEXT NOT NULL UNIQUE REFERENCES authority_retrievals(retrieval_id),
        sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
        provenance_json TEXT NOT NULL, file_metadata_json TEXT NOT NULL, created_at REAL NOT NULL,
        UNIQUE(source_id,item_id))""",
    """CREATE TABLE IF NOT EXISTS collection_cursors (
        source_id TEXT NOT NULL, item_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL UNIQUE REFERENCES collection_artifacts(artifact_id),
        source_revision INTEGER NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>0),
        PRIMARY KEY(source_id,item_id), UNIQUE(source_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS collection_results (
        result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES authority_runs(run_id),
        status TEXT NOT NULL CHECK(status IN('Pending','Unknown','Completed')),
        result_json TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS collection_run_enumerations (
        run_id TEXT PRIMARY KEY REFERENCES authority_runs(run_id), source_id TEXT NOT NULL,
        source_revision INTEGER NOT NULL, item_count INTEGER NOT NULL CHECK(item_count>=0))""",
    """CREATE TABLE IF NOT EXISTS collection_run_items (
        run_id TEXT NOT NULL REFERENCES collection_run_enumerations(run_id), ordinal INTEGER NOT NULL,
        item_id TEXT NOT NULL, provenance_json TEXT NOT NULL,
        PRIMARY KEY(run_id,item_id), UNIQUE(run_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS collection_run_artifacts (
        run_id TEXT NOT NULL REFERENCES authority_runs(run_id),
        artifact_id TEXT NOT NULL REFERENCES collection_artifacts(artifact_id),
        PRIMARY KEY(run_id,artifact_id))""",
)


class CollectionRepository:
    """Composable collection metadata; no connection, IO or callback ownership."""

    def __init__(self, uow):
        if not isinstance(uow, UnitOfWork):
            raise TypeError("CollectionRepository requires an explicit UnitOfWork")
        _ = uow.connection
        self.uow = uow

    @staticmethod
    def initialize(uow):
        repo = CollectionRepository(uow)
        for statement in _SCHEMA:
            repo.uow.connection.execute(statement)

    def run_items(self, context, source):
        row = self.uow.connection.execute("SELECT * FROM collection_run_enumerations WHERE run_id=?", (context.run_id,)).fetchone()
        if row is None:
            return None
        if row["source_id"] != source.source_id or row["source_revision"] != source.revision:
            raise BindingConflict("run enumeration belongs to another source revision")
        items = tuple(SourceItem(row["item_id"], row["provenance_json"]) for row in self.uow.connection.execute("SELECT * FROM collection_run_items WHERE run_id=? ORDER BY ordinal", (context.run_id,)))
        if len(items) != row["item_count"]:
            raise BindingConflict("run enumeration is incomplete")
        return items

    def record_enumeration(self, context, source, items):
        prior = self.run_items(context, source)
        if prior is not None:
            if tuple(items) != prior:
                raise BindingConflict("the original observed run item set is immutable")
            return prior
        if len({item.item_id for item in items}) != len(items):
            raise BindingConflict("observed item identities must be unique")
        self.uow.connection.execute("INSERT INTO collection_run_enumerations VALUES(?,?,?,?)", (context.run_id, source.source_id, source.revision, len(items)))
        for ordinal, item in enumerate(items):
            self.uow.connection.execute("INSERT INTO collection_run_items VALUES(?,?,?,?)", (context.run_id, ordinal, item.item_id, item.provenance_json))
        return tuple(items)

    def run_artifacts(self, context, source):
        """Charge prior publications and reused artifacts in the original set.

        The membership table also retains local-folder artifacts after files
        disappear and binds cached artifacts to a later run without changing
        their original provenance. Each artifact appears exactly once.
        """
        rows = self.uow.connection.execute("""SELECT a.* FROM collection_artifacts a
            WHERE a.source_id=? AND (a.run_id=?
                OR a.artifact_id IN (SELECT artifact_id FROM collection_run_artifacts WHERE run_id=?)
                OR a.item_id IN (SELECT item_id FROM collection_run_items WHERE run_id=?))
            ORDER BY a.created_at,a.artifact_id""",
            (source.source_id, context.run_id, context.run_id, context.run_id))
        return tuple(Artifact(**dict(row)) for row in rows)

    def remaining_quota(self, context, source):
        max_bytes, max_items = _limits(source)
        artifacts = self.run_artifacts(context, source)
        return max_bytes - sum(item.size for item in artifacts), max_items - len(artifacts)

    def assert_quota(self, context, source, *, item_id=None, size=0):
        remaining_bytes, remaining_items = self.remaining_quota(context, source)
        if item_id is not None:
            existing = self.artifact(source.source_id, item_id)
            charged = {item.artifact_id for item in self.run_artifacts(context, source)}
            if existing is None or existing.artifact_id not in charged:
                remaining_bytes -= existing.size if existing else size
                remaining_items -= 1
        if remaining_bytes < 0 or remaining_items < 0:
            raise CollectionQuotaExceeded("Original run byte or item quota is exhausted")

    def remember_artifact(self, context, source, artifact):
        self.assert_quota(context, source, item_id=artifact.item_id, size=artifact.size)
        self.uow.connection.execute("INSERT OR IGNORE INTO collection_run_artifacts VALUES(?,?)",
                                   (context.run_id, artifact.artifact_id))

    @staticmethod
    def _context_json(context):
        # Desktop ownership belongs to each short dispatch. The durable item
        # lineage survives acquiring and releasing that ephemeral generation.
        identity = asdict(context)
        identity["desktop_fence"] = None
        return canonical_json(identity)

    def _binding(self, context, source):
        if not isinstance(context, RunContext) or not isinstance(source, SourceSpec):
            raise TypeError("collection requires immutable RunContext and SourceSpec")
        if (context.source_id, context.source_revision) != (source.source_id, source.revision):
            raise BindingConflict("Source does not belong to this run")
        db = self.uow.connection
        stored = db.execute("SELECT * FROM authority_sources WHERE source_id=? AND revision=?",
                            (source.source_id, source.revision)).fetchone()
        run = db.execute("SELECT * FROM authority_runs WHERE run_id=?", (context.run_id,)).fetchone()
        execution = db.execute("SELECT * FROM execution_contexts WHERE run_id=?", (context.run_id,)).fetchone()
        if (stored is None or stored["kind"] != source.kind or stored["scope_json"] != source.scope_json
                or run is None or execution is None):
            raise BindingConflict("Source or run binding is missing or changed")
        for key in ("grant_id", "source_id", "source_revision", "job_id", "job_revision",
                    "occurrence_id", "lifecycle_id", "epoch", "run_fence"):
            if run[key] != getattr(context, key):
                raise BindingConflict("Collection run lineage changed")
        for key in ("session", "admission_id", "task_id", "input_event_id", "lifecycle_id",
                    "epoch", "run_fence", "desktop_fence"):
            if execution[key] != getattr(context, key):
                raise BindingConflict("Execution lineage changed")

    def hazard(self, source_id):
        row = self.uow.connection.execute("SELECT unresolved_retrieval_id FROM source_guard WHERE source_id=?",
                                         (source_id,)).fetchone()
        if row is None:
            raise BindingConflict("Unknown source")
        return row[0]

    def retrieval(self, retrieval_id):
        row = self.uow.connection.execute("""SELECT r.*,m.source_revision,m.item_provenance_json,m.context_json
            FROM authority_retrievals r JOIN collection_retrieval_metadata m USING(retrieval_id)
            WHERE retrieval_id=?""", (retrieval_id,)).fetchone()
        return dict(row) if row else None

    def artifact(self, source_id, item_id):
        row = self.uow.connection.execute("SELECT * FROM collection_artifacts WHERE source_id=? AND item_id=?",
                                         (source_id, item_id)).fetchone()
        return Artifact(**dict(row)) if row else None

    def prepare_retrieval(self, context, source, item, retrieval_id):
        identifier(retrieval_id, "retrieval_id")
        if not isinstance(item, SourceItem):
            raise TypeError("retrieval requires an immutable SourceItem")
        hazard = self.hazard(source.source_id)
        if hazard is not None and hazard != retrieval_id:
            raise BindingConflict("Source has an unresolved retrieval")
        self._binding(context, source)
        db = self.uow.connection
        existing = self.retrieval(retrieval_id)
        if existing is None:
            prior = db.execute("SELECT retrieval_id FROM authority_retrievals WHERE source_id=? AND item_id=?",
                               (source.source_id, item.item_id)).fetchone()
            if prior:
                existing = self.retrieval(prior[0])
                if existing is None or existing["state"] != "NoEffect":
                    raise BindingConflict("Source item already has a durable retrieval identity")
        context_json = self._context_json(context)
        if existing:
            if (existing["source_id"] != source.source_id or existing["item_id"] != item.item_id
                    or existing["item_provenance_json"] != item.provenance_json):
                raise BindingConflict("Retrieval identity is already bound")
            if existing["state"] == "NoEffect":
                proof = db.execute("SELECT * FROM collection_no_effect_evidence WHERE retrieval_id=?",
                                   (existing["retrieval_id"],)).fetchone()
                if (proof is None or proof["operation_id"] != existing["operation_id"]
                        or json.loads(proof["evidence_json"]).get("no_effect") is not True):
                    raise BindingConflict("NoEffect retrieval lacks confirmed no-effect proof")
                if existing["operation_id"] is not None:
                    operation = db.execute("SELECT * FROM operations WHERE operation_id=?",
                                           (existing["operation_id"],)).fetchone()
                    if (operation is None or operation["state"] != "Failed"
                            or json.loads(operation["result_json"]).get("no_effect") is not True):
                        raise BindingConflict("Original operation is not confirmed without effect")
                original_id = existing["retrieval_id"]
                db.execute("""INSERT INTO collection_retrieval_history
                    (retrieval_id,run_id,operation_id,source_revision,context_json,item_provenance_json,state,evidence_json)
                    VALUES(?,?,?,?,?,?,'NoEffect',?)""", (original_id, existing["run_id"], existing["operation_id"],
                    existing["source_revision"], existing["context_json"], existing["item_provenance_json"], proof["evidence_json"]))
                db.execute("UPDATE authority_retrievals SET run_id=?,operation_id=NULL,state='Prepared' WHERE retrieval_id=?",
                           (context.run_id, original_id))
                db.execute("UPDATE collection_retrieval_metadata SET source_revision=?,context_json=? WHERE retrieval_id=?",
                           (source.revision, context_json, original_id))
                db.execute("DELETE FROM collection_no_effect_evidence WHERE retrieval_id=?", (original_id,))
                db.execute("UPDATE source_guard SET unresolved_retrieval_id=? WHERE source_id=?", (original_id, source.source_id))
                return self.retrieval(original_id)
            if (existing["source_id"] != source.source_id or existing["item_id"] != item.item_id
                    or existing["context_json"] != context_json
                    or existing["item_provenance_json"] != item.provenance_json):
                raise BindingConflict("Retrieval identity is already bound")
            return existing
        if db.execute("SELECT 1 FROM authority_retrievals WHERE source_id=? AND item_id=?",
                      (source.source_id, item.item_id)).fetchone():
            raise BindingConflict("Source item already has a durable retrieval identity")
        db.execute("INSERT INTO authority_retrievals VALUES(?,?,?,?,NULL,'Prepared')",
                   (retrieval_id, source.source_id, item.item_id, context.run_id))
        db.execute("INSERT INTO collection_retrieval_metadata VALUES(?,?,?,?)",
                   (retrieval_id, source.revision, item.provenance_json, context_json))
        db.execute("UPDATE source_guard SET unresolved_retrieval_id=? WHERE source_id=?",
                   (retrieval_id, source.source_id))
        return self.retrieval(retrieval_id)

    def _exact(self, context, source, retrieval_id):
        self._binding(context, source)
        row = self.retrieval(retrieval_id)
        if (row is None or row["context_json"] != self._context_json(context)
                or row["source_id"] != source.source_id or row["source_revision"] != source.revision):
            raise BindingConflict("Retrieval does not belong to this exact run/source")
        return row

    def _operation(self, context, operation_id):
        row = self.uow.connection.execute("""SELECT o.*,x.run_id,x.attempt_id AS bound_attempt
            FROM operations o JOIN execution_operations x USING(operation_id) WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if (row is None or row["run_id"] != context.run_id or row["task_id"] != context.task_id
                or row["attempt_id"] != row["bound_attempt"]):
            raise BindingConflict("Retrieval operation lacks the exact execution binding")
        return row

    def bind_operation(self, context, source, retrieval_id, operation_id):
        row = self._exact(context, source, retrieval_id)
        operation = self._operation(context, operation_id)
        if (row["operation_id"] not in (None, operation_id)
                or row["state"] not in ("Prepared", "Applying")
                or operation["state"] != "Applying"
                or self.hazard(source.source_id) != retrieval_id):
            raise BindingConflict("Retrieval cannot bind this applying operation")
        self.uow.connection.execute("UPDATE authority_retrievals SET operation_id=?,state='Applying' WHERE retrieval_id=?",
                                   (operation_id, retrieval_id))

    def candidate_matches(self, context, source, retrieval_id, candidate):
        row = self._exact(context, source, retrieval_id)
        if row["state"] not in ("Applying", "Unknown") or not row["operation_id"]:
            return False
        operation = self._operation(context, row["operation_id"])
        observed = json.loads(row["item_provenance_json"])
        expected = {**observed, "run_id": context.run_id, "retrieval_id": retrieval_id,
                    "operation_id": row["operation_id"]}
        return (operation["state"] in ("Applying", "Unknown")
                and self.hazard(source.source_id) == retrieval_id
                and candidate.item_id == row["item_id"] and candidate.provenance == expected
                and all(observed.get(key) == source.scope.get(key)
                        for key in ("app", "account", "conversation"))
                and observed.get("source_item") == candidate.item_id)

    def mark_unknown(self, context, source, retrieval_id):
        row = self._exact(context, source, retrieval_id)
        if row["state"] not in ("Applying", "Unknown"):
            return
        self.uow.connection.execute("UPDATE authority_retrievals SET state='Unknown' WHERE retrieval_id=?",
                                   (retrieval_id,))
        if row["operation_id"]:
            self.uow.connection.execute("UPDATE operations SET state='Unknown' WHERE operation_id=? AND state='Applying'",
                                       (row["operation_id"],))

    def confirm_no_effect(self, context, source, retrieval_id, operation_id, evidence):
        row = self._exact(context, source, retrieval_id)
        if operation_id is not None:
            operation = self._operation(context, operation_id)
            if operation["state"] == "Failed":
                if (type(evidence) is not dict or evidence.get("no_effect") is not True
                        or operation["result_json"] != canonical_json(evidence)):
                    raise BindingConflict("Failed operation requires its exact persisted no-effect receipt")
            elif operation["state"] not in ("Applying", "Unknown"):
                raise BindingConflict("Operation is not awaiting no-effect reconciliation")
        allowed_states = ("Prepared",) if operation_id is None else ("Applying", "Unknown")
        if (row["operation_id"] != operation_id or row["state"] not in allowed_states
                or type(evidence) is not dict or evidence.get("no_effect") is not True):
            raise BindingConflict("No-effect evidence does not bind this retrieval/operation")
        self.uow.connection.execute("UPDATE authority_retrievals SET state='NoEffect' WHERE retrieval_id=?", (retrieval_id,))
        self.uow.connection.execute("INSERT OR REPLACE INTO collection_no_effect_evidence VALUES(?,?,?)",
                                   (retrieval_id, operation_id, canonical_json(evidence)))
        if operation_id is not None:
            self.uow.connection.execute("UPDATE operations SET state='Failed',result_json=? WHERE operation_id=?",
                                       (canonical_json(evidence), operation_id))
        self._clear_hazard(source.source_id, retrieval_id)

    def _clear_hazard(self, source_id, retrieval_id):
        self.uow.connection.execute("UPDATE source_guard SET unresolved_retrieval_id=NULL WHERE source_id=? AND unresolved_retrieval_id=?",
                                   (source_id, retrieval_id))

    def publish(self, context, source, retrieval_id, candidate, digest, size, metadata, *, now):
        self._exact(context, source, retrieval_id)
        self.assert_quota(context, source, item_id=candidate.item_id, size=size)
        existing = self.artifact(source.source_id, candidate.item_id)
        if existing:
            self.remember_artifact(context, source, existing)
            return existing
        if source.kind == "desktop_attachments" and not self.candidate_matches(context, source, retrieval_id, candidate):
            raise BindingConflict("Desktop candidate has no exact durable operation provenance")
        artifact = Artifact(str(uuid.uuid4()), source.source_id, source.revision, candidate.item_id,
            context.run_id, retrieval_id, digest, size, candidate.provenance_json, canonical_json(metadata), now)
        db = self.uow.connection
        db.execute("INSERT INTO collection_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(asdict(artifact).values()))
        self.remember_artifact(context, source, artifact)
        ordinal = db.execute("SELECT coalesce(max(ordinal),0)+1 FROM collection_cursors WHERE source_id=?",
                             (source.source_id,)).fetchone()[0]
        db.execute("INSERT INTO collection_cursors VALUES(?,?,?,?,?)",
                   (source.source_id, candidate.item_id, artifact.artifact_id, source.revision, ordinal))
        row = self.retrieval(retrieval_id)
        db.execute("UPDATE authority_retrievals SET state='Verified' WHERE retrieval_id=?", (retrieval_id,))
        if row["operation_id"]:
            db.execute("UPDATE operations SET state='Applied',result_json=? WHERE operation_id=?",
                       (canonical_json({"artifact_id": artifact.artifact_id, "sha256": digest}), row["operation_id"]))
        self._clear_hazard(source.source_id, retrieval_id)
        return artifact

    def record_result(self, context, result_id, result, *, now):
        identifier(result_id, "result_id")
        old = self.uow.connection.execute("SELECT run_id FROM collection_results WHERE result_id=?",
                                         (result_id,)).fetchone()
        if old is not None and old["run_id"] != context.run_id:
            raise BindingConflict("Result identifier belongs to another run")
        self.uow.connection.execute("""INSERT INTO collection_results VALUES(?,?,?,?,?)
            ON CONFLICT(result_id) DO UPDATE SET status=excluded.status,result_json=excluded.result_json,updated_at=excluded.updated_at""",
            (result_id, context.run_id, result.status, canonical_json(result.as_dict()), now))

    @staticmethod
    def item_result_id(context, retrieval_id):
        """One result per run/retrieval; no-effect retries retain older outcomes."""
        identifier(retrieval_id, "retrieval_id")
        return "item:" + canonical_json([context.run_id, retrieval_id])


class _CandidateRejected(ValueError):
    pass


def _directory(path):
    """Open every absolute component without following any symbolic link."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise _CandidateRejected("Configured root must be an absolute contained directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


class CollectionService:
    """All reads/publications pass the common coordinator; never finish its run."""

    def __init__(self, store, artifact_root, coordinator, desktop=None):
        self.store, self.coordinator, self.desktop = store, coordinator, desktop
        self.artifact_root = Path(artifact_root)
        if not self.artifact_root.is_absolute() or ".." in self.artifact_root.parts:
            raise ValueError("artifact_root must be absolute")
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = _directory(self.artifact_root)
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                raise ValueError("artifact_root must be owned by this user")
        finally:
            os.close(fd)
        with self.store.transaction() as unit:
            CollectionRepository.initialize(unit)

    async def _scope(self, context, source):
        if not isinstance(context, RunContext) or not isinstance(source, SourceSpec):
            raise TypeError("collection requires immutable RunContext and SourceSpec")
        if source != await self.coordinator.source(context):
            raise BindingConflict("Caller source differs from server-stored scope")
        scope = source.scope
        root = Path(scope.get("root", ""))
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("Source root must be an absolute directory")
        for key, default in (("max_items", 100), ("max_bytes", 64 * 1024 * 1024)):
            value = scope.get(key, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
            scope[key] = value
        if type(scope.get("filename_glob", "*")) is not str:
            raise ValueError("filename_glob must be text")
        return root, scope

    async def _check(self, context, phase):
        await self.coordinator.checkpoint(context, phase)
        now = time.time()
        with self.store.transaction() as unit:
            self.coordinator.validate_local(unit, context, phase, now=now)

    async def _result(self, context, result_id, result):
        await self.coordinator.checkpoint(context, "collection_result")
        now = time.time()
        with self.store.transaction() as unit:
            self.coordinator.validate_local(unit, context, "collection_result", now=now)
            repo = CollectionRepository(unit)
            if result.status == "Completed":
                stored = unit.connection.execute("SELECT * FROM authority_sources WHERE source_id=? AND revision=?",
                    (context.source_id, context.source_revision)).fetchone()
                source = SourceSpec(stored["source_id"], stored["revision"], stored["kind"], stored["scope_json"])
                try:
                    repo.assert_quota(context, source)
                except CollectionQuotaExceeded as error:
                    result = CollectionResult("Pending", result.artifacts, str(error))
            repo.record_result(context, result_id, result, now=now)
        return result

    def _list(self, root, scope):
        paths, rejected = [], False
        budget = scope["max_items"]
        scan_budget = budget * 16

        def walk(fd, prefix):
            nonlocal rejected, scan_budget
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if scan_budget == 0:
                        rejected = True
                        break
                    scan_budget -= 1
                    names.append(entry.name)
            for name in sorted(names):
                relative = prefix / name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if len(relative.parts) > 64 or scan_budget == 0:
                        rejected = True
                        continue
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, relative)
                    finally:
                        os.close(child)
                elif fnmatch.fnmatchcase(str(relative), scope.get("filename_glob", "*")):
                    if len(paths) >= budget:
                        rejected = True
                        return
                    if not stat.S_ISREG(info.st_mode):
                        rejected = True
                    else:
                        paths.append(root / relative)
                if len(paths) >= budget and rejected:
                    return

        fd = _directory(root)
        try:
            walk(fd, Path())
        finally:
            os.close(fd)
        return paths, rejected

    @staticmethod
    def _local_candidate(source, root, path, digest, metadata):
        item_id = hashlib.sha256(canonical_json({"path": metadata["relative_path"], "sha256": digest,
            "identity": metadata["before"]}).encode()).hexdigest()
        return FileCandidate(path, item_id, canonical_json({"kind": "local_files",
            "source_id": source.source_id, "root": str(root), "relative_path": metadata["relative_path"]}), True, True)

    def _cached_local(self, source, root, path, artifacts):
        """Revalidate containment and the unchanged snapshot without recopying."""
        if not path.is_absolute() or ".." in path.parts:
            raise _CandidateRejected("Candidate path is outside configured root")
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise _CandidateRejected("Candidate path is outside configured root") from error
        candidates = [item for item in artifacts if json.loads(item.file_metadata_json)["relative_path"] == str(relative)]
        if not candidates:
            return None
        directory, descriptor = _directory(root), None
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            before = os.fstat(descriptor)
            after = os.fstat(descriptor)
            path_after = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or _identity(before) != _identity(after) or _identity(before) != _identity(path_after):
                raise _CandidateRejected("Cached file changed during snapshot validation")
            metadata = {"relative_path": str(relative), "before": _identity(before), "after": _identity(after)}
            for artifact in candidates:
                if json.loads(artifact.file_metadata_json)["before"] == metadata["before"]:
                    candidate = self._local_candidate(source, root, path, artifact.sha256, metadata)
                    if artifact.item_id == candidate.item_id and artifact.provenance_json == candidate.provenance_json:
                        return artifact
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    def _snapshot(self, root, scope, path):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise _CandidateRejected("Candidate path is outside configured root")
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise _CandidateRejected("Candidate path is outside configured root") from error
        if not relative.parts or not fnmatch.fnmatchcase(str(relative), scope.get("filename_glob", "*")):
            raise _CandidateRejected("Candidate does not qualify for this source")
        if relative.name.lower().endswith((".part", ".partial", ".tmp", ".crdownload", ".download")):
            raise _CandidateRejected("Qualifying temporary file is still pending")
        directory = _directory(root)
        source_fd = temporary_fd = blob_fd = None
        temporary = ".pending-" + str(uuid.uuid4())
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            source_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > scope["max_bytes"]:
                raise _CandidateRejected("Candidate must be a bounded regular file")
            blob_fd = _directory(self.artifact_root)
            temporary_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=blob_fd)
            digest, size = hashlib.sha256(), 0
            while chunk := os.read(source_fd, min(1024 * 1024, scope["max_bytes"] + 1)):
                size += len(chunk)
                if size > scope["max_bytes"]:
                    raise _CandidateRejected("Candidate exceeded byte bound during copy")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_fd, view)
                    view = view[written:]
            after = os.fstat(source_fd)
            path_after = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
            if _identity(before) != _identity(after) or _identity(before) != _identity(path_after) or size != before.st_size:
                raise _CandidateRejected("Candidate changed during bounded snapshot")
            os.fsync(temporary_fd)
            address = digest.hexdigest()
            os.close(temporary_fd)
            temporary_fd = None
            try:
                os.link(temporary, address, src_dir_fd=blob_fd, dst_dir_fd=blob_fd, follow_symlinks=False)
            except FileExistsError:
                existing_fd = os.open(address, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=blob_fd)
                try:
                    existing_info = os.fstat(existing_fd)
                    if not stat.S_ISREG(existing_info.st_mode) or existing_info.st_size != size:
                        raise _CandidateRejected("Existing content address is invalid")
                    existing_hash = hashlib.sha256()
                    while chunk := os.read(existing_fd, 1024 * 1024):
                        existing_hash.update(chunk)
                    if existing_hash.hexdigest() != address:
                        raise _CandidateRejected("Existing content address has different bytes")
                finally:
                    os.close(existing_fd)
            os.unlink(temporary, dir_fd=blob_fd)
            os.fsync(blob_fd)
            return address, size, {"relative_path": str(relative), "before": _identity(before), "after": _identity(after)}
        finally:
            for fd in (source_fd, temporary_fd, directory):
                if fd is not None:
                    os.close(fd)
            if blob_fd is not None:
                try:
                    os.unlink(temporary, dir_fd=blob_fd)
                except FileNotFoundError:
                    pass
                os.close(blob_fd)

    async def collect(self, context, source):
        root, scope = await self._scope(context, source)
        await self._check(context, "collection_read")
        with self.store._read_unit() as unit:
            hazard = CollectionRepository(unit).hazard(source.source_id)
        if hazard:
            return await self._result(context, "run:" + context.run_id,
                                      CollectionResult("Unknown", reason="Source has an unresolved retrieval"))
        artifacts, status, reason = [], "Completed", ""
        if source.kind == "local_files":
            with self.store._read_unit() as unit:
                artifacts = list(CollectionRepository(unit).run_artifacts(context, source))
            try:
                paths, rejected = self._list(root, scope)
            except (OSError, _CandidateRejected) as error:
                return await self._result(context, "run:" + context.run_id, CollectionResult("Pending", reason=str(error)))
            if rejected:
                status, reason = "Pending", "Some qualifying entries were rejected or exceeded bounds"
            for path in paths:
                await self._check(context, "collection_read")
                try:
                    cached = self._cached_local(source, root, path, artifacts)
                    if cached is not None:
                        continue
                    with self.store._read_unit() as unit:
                        remaining, slots = CollectionRepository(unit).remaining_quota(context, source)
                    if remaining < 0 or slots <= 0:
                        raise CollectionQuotaExceeded("Original run byte or item quota is exhausted")
                    digest, size, metadata = self._snapshot(root, {**scope, "max_bytes": remaining}, path)
                    candidate = self._local_candidate(source, root, path, digest, metadata)
                except (OSError, _CandidateRejected, CollectionQuotaExceeded) as error:
                    status, reason = "Pending", str(error)
                    continue
                try:
                    artifact = await self._publish(context, source, "local:" + candidate.item_id, candidate, digest, size, metadata)
                except CollectionQuotaExceeded as error:
                    status, reason = "Pending", str(error)
                    continue
                if artifact.artifact_id not in {item.artifact_id for item in artifacts}:
                    artifacts.append(artifact)
        else:
            if self.desktop is None:
                return await self._result(context, "run:" + context.run_id,
                                          CollectionResult("Pending", reason="Selected desktop retrieval port is unavailable"))
            with self.store._read_unit() as unit:
                items = CollectionRepository(unit).run_items(context, source)
            if items is None:
                items = await self.desktop.enumerate(context, source)
            if not isinstance(items, (list, tuple)) or len(items) > scope["max_items"] or any(not isinstance(item, SourceItem) for item in items):
                raise ValueError("Desktop enumeration must return a bounded sequence of SourceItem values")
            if any(any(item.provenance.get(key) != scope.get(key) for key in ("app", "account", "conversation")) or item.provenance.get("source_item") != item.item_id for item in items):
                return await self._result(context, "run:" + context.run_id, CollectionResult("Unknown", reason="Observed item does not match configured desktop source"))
            await self._check(context, "collection-enumeration")
            with self.store.transaction() as unit:
                self.coordinator.validate_local(unit, context, "collection-enumeration", now=time.time())
                items = CollectionRepository(unit).record_enumeration(context, source, items)
            for item in items:
                if (any(not isinstance(scope.get(key), str) or not scope[key]
                        or item.provenance.get(key) != scope[key]
                        for key in ("app", "account", "conversation"))
                        or item.provenance.get("source_item") != item.item_id):
                    status, reason = "Unknown", "Observed item does not match configured desktop source"
                    break
                await self.coordinator.checkpoint(context, "collection_prepare")
                now, retrieval_id = time.time(), str(uuid.uuid4())
                with self.store.transaction() as unit:
                    self.coordinator.validate_local(unit, context, "collection_prepare", now=now)
                    repo = CollectionRepository(unit)
                    existing = repo.artifact(source.source_id, item.item_id)
                    if existing:
                        observed = {key: value for key, value in json.loads(existing.provenance_json).items()
                                    if key not in ("run_id", "retrieval_id", "operation_id")}
                        if canonical_json(observed) != item.provenance_json:
                            status, reason = "Unknown", "Source item identity has changed provenance"
                            break
                        artifacts.append(existing)
                        continue
                    remaining, slots = repo.remaining_quota(context, source)
                    if remaining < 0 or slots <= 0:
                        status, reason = "Pending", "Collection byte budget is exhausted"
                        break
                    retrieval_id = repo.prepare_retrieval(context, source, item, retrieval_id)["retrieval_id"]
                try:
                    candidate = await self.desktop.retrieve(context, source, retrieval_id, item)
                    result = await self._ingest(context, source, retrieval_id, candidate, byte_limit=remaining)
                except BaseException:
                    with self.store.transaction() as unit:
                        CollectionRepository(unit).mark_unknown(context, source, retrieval_id)
                    raise
                artifacts.extend(result.artifacts)
                if result.status != "Completed":
                    status, reason = result.status, result.reason
                    break
        return await self._result(context, "run:" + context.run_id, CollectionResult(status, tuple(artifacts), reason))

    async def _publish(self, context, source, retrieval_id, candidate, digest, size, metadata):
        await self.coordinator.checkpoint(context, "collection_publish")
        now = time.time()
        with self.store.transaction() as unit:
            self.coordinator.validate_local(unit, context, "collection_publish", now=now)
            repo = CollectionRepository(unit)
            existing = repo.artifact(source.source_id, candidate.item_id)
            repo.assert_quota(context, source, item_id=candidate.item_id, size=size)
            if existing:
                repo.remember_artifact(context, source, existing)
                return existing
            if source.kind == "local_files":
                repo.prepare_retrieval(context, source, SourceItem(candidate.item_id, candidate.provenance_json), retrieval_id)
            artifact = repo.publish(context, source, retrieval_id, candidate, digest, size, metadata, now=now)
            repo.record_result(context, repo.item_result_id(context, retrieval_id),
                               CollectionResult("Completed", (artifact,)), now=now)
            return artifact

    async def ingest(self, context, source, retrieval_id, candidate):
        return await self._ingest(context, source, retrieval_id, candidate)

    async def ingest_run(self, context, source, retrieval_id, candidate):
        """Publish one candidate and report the original whole-run progress.

        This resumes observation/publication only; remaining items require an
        explicit collect call and cannot disappear from the persisted set.
        """
        item_result = await self.ingest(context, source, retrieval_id, candidate)
        await self._check(context, "collection-run-progress")
        with self.store._read_unit() as unit:
            self.coordinator.validate_local(unit, context, "collection-run-progress", now=time.time())
            repo = CollectionRepository(unit)
            items = repo.run_items(context, source)
            artifacts, missing = [], 0
            if items is not None:
                for item in items:
                    artifact = repo.artifact(source.source_id, item.item_id)
                    observed = ({key: value for key, value in json.loads(artifact.provenance_json).items() if key not in ("run_id", "retrieval_id", "operation_id")} if artifact else None)
                    if artifact and canonical_json(observed) == item.provenance_json:
                        artifacts.append(artifact)
                    else:
                        missing += 1
            hazard = repo.hazard(source.source_id)
        if item_result.status != "Completed":
            result = CollectionResult(item_result.status, tuple(artifacts), item_result.reason)
        elif items is None:
            result = CollectionResult("Pending", tuple(artifacts), "Original observed item set is unavailable")
        elif missing or hazard:
            result = CollectionResult("Pending", tuple(artifacts), "Original run still has unverified items")
        else:
            result = CollectionResult("Completed", tuple(artifacts))
        return await self._result(context, "run:" + context.run_id, result)

    async def _ingest(self, context, source, retrieval_id, candidate, *, byte_limit=None):
        root, scope = await self._scope(context, source)
        with self.store._read_unit() as unit:
            remaining, slots = CollectionRepository(unit).remaining_quota(context, source)
        scope["max_bytes"] = min(scope["max_bytes"], max(0, remaining))
        if byte_limit is not None:
            scope["max_bytes"] = min(scope["max_bytes"], byte_limit)
        if not isinstance(candidate, FileCandidate):
            raise TypeError("ingest requires an immutable FileCandidate")
        await self._check(context, "collection_read")
        if source.kind == "desktop_attachments":
            try:
                with self.store._read_unit() as unit:
                    repo = CollectionRepository(unit)
                    row = repo._exact(context, source, retrieval_id)
                    if row["state"] == "NoEffect":
                        proof = unit.connection.execute("SELECT * FROM collection_no_effect_evidence WHERE retrieval_id=?", (retrieval_id,)).fetchone()
                        operation = repo._operation(context, row["operation_id"]) if row["operation_id"] else None
                        if (proof and proof["operation_id"] == row["operation_id"] and json.loads(proof["evidence_json"]).get("no_effect") is True
                                and (operation is None or (operation["state"] == "Failed" and operation["result_json"] == proof["evidence_json"]))):
                            return CollectionResult("Pending", reason="Confirmed no effect; original retrieval may safely resume")
                    existing = repo.artifact(source.source_id, candidate.item_id)
                    if (row["state"] == "Verified" and existing
                            and existing.retrieval_id == retrieval_id
                            and existing.provenance_json == candidate.provenance_json):
                        return CollectionResult("Completed", (existing,))
                    matched = repo.candidate_matches(context, source, retrieval_id, candidate)
            except BindingConflict:
                matched = False
            if not matched or not candidate.correlated:
                return await self._result(context, CollectionRepository.item_result_id(context, retrieval_id),
                                          CollectionResult("Unknown", reason="Download item/operation correlation is unproven"))
            if not candidate.source_complete:
                return await self._result(context, CollectionRepository.item_result_id(context, retrieval_id),
                                          CollectionResult("Pending", reason="Source completion is unproven"))
        try:
            if source.kind == "local_files" and remaining >= 0 and slots >= 0:
                with self.store._read_unit() as unit:
                    known = CollectionRepository(unit).run_artifacts(context, source)
                cached = self._cached_local(source, root, candidate.path, known)
                if cached is not None:
                    if (candidate.observed_identity_json is not None
                            and candidate.observed_identity_json != canonical_json(json.loads(cached.file_metadata_json)["before"])):
                        raise _CandidateRejected("Candidate changed after source correlation")
                    if candidate.expected_sha256 is not None and candidate.expected_sha256 != cached.sha256:
                        raise _CandidateRejected("Candidate bytes do not match the observed source digest")
                    return CollectionResult("Completed", (cached,))
            if remaining < 0 or slots <= 0:
                raise CollectionQuotaExceeded("Original run byte or item quota is exhausted")
            digest, size, metadata = self._snapshot(root, scope, candidate.path)
            if candidate.observed_identity_json is not None and canonical_json(metadata["before"]) != candidate.observed_identity_json:
                raise _CandidateRejected("Candidate changed after source correlation")
            if candidate.expected_sha256 is not None and digest != candidate.expected_sha256:
                raise _CandidateRejected("Candidate bytes do not match the observed source digest")
        except (OSError, _CandidateRejected, CollectionQuotaExceeded) as error:
            return await self._result(context, CollectionRepository.item_result_id(context, retrieval_id),
                                      CollectionResult("Pending", reason=str(error)))
        if source.kind == "local_files":
            candidate = self._local_candidate(source, root, candidate.path, digest, metadata)
            retrieval_id = "local:" + candidate.item_id
        try:
            artifact = await self._publish(context, source, retrieval_id, candidate, digest, size, metadata)
        except CollectionQuotaExceeded as error:
            return await self._result(context, CollectionRepository.item_result_id(context, retrieval_id),
                                      CollectionResult("Pending", reason=str(error)))
        return CollectionResult("Completed", (artifact,))
