"""Core-selected AX attachment recipes; observed completion is not file appearance.

Recipes are immutable operator source data, never executable model callbacks.
The final owner-held observation supplies the durable pre-input baseline. Resume
only observes that original retrieval and cannot issue another uncertain input.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from .collection import CollectionRepository, FileCandidate, SourceItem, _directory, _identity
from .contracts import BindingConflict, canonical_json, identifier
from .execution import DesktopExecutor, make_effect_spec


MAX_RECIPE_BYTES = 64 * 1024
MAX_ITEMS = 100
MAX_AX_NODES = 250
MAX_TEXT_BYTES = 4096
RECIPE_NAME = "desktop-attachment-v1"


def _text(value, label, maximum=MAX_TEXT_BYTES):
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum or "\0" in value:
        raise ValueError(f"{label} must be bounded nonempty text")
    return value


def _selector(value):
    if (type(value) is not dict or set(value) - {"role", "identifier", "text"}
            or not {"role", "text"} <= set(value)):
        raise ValueError("selector requires exact role/text and optional identifier")
    _text(value["role"], "selector role")
    _text(value["text"], "selector text")
    if "identifier" in value:
        _text(value["identifier"], "selector identifier")


def _recipe(source):
    scope = source.scope
    if source.kind != "desktop_attachments" or scope.get("recipe") != RECIPE_NAME:
        raise ValueError("source requires the desktop-attachment-v1 recipe")
    for name in ("app", "account", "conversation"):
        _text(scope.get(name), name)
    root = Path(scope.get("root", ""))
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("desktop root must be absolute without parent traversal")
    config = scope.get("desktop")
    if (type(config) is not dict or set(config) != {"app_selector", "window_title",
            "account_selector", "conversation_selector", "items"}
            or len(canonical_json(config).encode("utf-8")) > MAX_RECIPE_BYTES):
        raise ValueError("desktop recipe has unsupported fields or exceeds bounds")
    app = config["app_selector"]
    if (type(app) is not dict or set(app) != {"field", "equals"}
            or app["field"] not in ("bundle_id", "name") or app["equals"] != scope["app"]):
        raise ValueError("app selector must exactly match the source app")
    _text(config["window_title"], "window title")
    for name in ("account", "conversation"):
        _selector(config[name + "_selector"])
        if config[name + "_selector"]["text"] != scope[name]:
            raise ValueError("identity selector text must exactly match source scope")
    items, limit = config["items"], scope.get("max_items", MAX_ITEMS)
    if (type(limit) is not int or limit < 1 or type(items) is not list
            or not 1 <= len(items) <= min(MAX_ITEMS, limit)):
        raise ValueError("recipe must contain a bounded nonempty item list")
    ids = set()
    for item in items:
        if (type(item) is not dict or set(item) - {"item_id", "item_selector", "download_selector",
                "filename", "completion", "expected_size", "expected_sha256"}
                or not {"item_id", "item_selector", "download_selector", "filename", "completion"} <= set(item)):
            raise ValueError("attachment item has unsupported or missing fields")
        identifier(item["item_id"], "item_id")
        if item["item_id"] in ids:
            raise ValueError("recipe item identities must be unique")
        ids.add(item["item_id"])
        filename = _text(item["filename"], "filename", 255)
        if filename in (".", "..") or "/" in filename or "\\" in filename or filename.lower().endswith((".part", ".partial", ".tmp", ".crdownload", ".download")):
            raise ValueError("filename must name one completed regular file")
        for key in ("item_selector", "download_selector"):
            _selector(item[key])
        completion = item["completion"]
        if type(completion) is not dict or set(completion) != {"before", "after"}:
            raise ValueError("completion requires an explicit before/after transition")
        for state in ("before", "after"):
            _selector(completion[state])
        before, after = completion["before"], completion["after"]
        if ({k: v for k, v in before.items() if k != "text"} != {k: v for k, v in after.items() if k != "text"}
                or before["text"] == after["text"] or filename not in after["text"]):
            raise ValueError("completion must change the same node and name the exact filename")
        if "expected_size" in item and (type(item["expected_size"]) is not int or item["expected_size"] < 0
                or item["expected_size"] > scope.get("max_bytes", 64 * 1024 * 1024)):
            raise ValueError("expected_size must fit the source byte budget")
        digest = item.get("expected_sha256")
        if digest is not None and (type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("expected_sha256 must be a canonical digest")
    return root, config, hashlib.sha256(canonical_json(config).encode()).hexdigest()


def _matches(node, query):
    return (node.get("role") == query["role"]
            and ("identifier" not in query or node.get("identifier") == query["identifier"])
            and query["text"] in [node.get(key) for key in ("label", "title", "value", "description")])


def _unique(nodes, query, *, optional=False):
    matches = [node for node in nodes if _matches(node, query)]
    if not matches and optional:
        return None
    if len(matches) != 1:
        raise BindingConflict("AX selector is missing or ambiguous")
    return matches[0]


def _anchor(node):
    geometry = node.get("geometry")
    if type(geometry) is not dict or set(geometry) != {"x", "y", "width", "height"}:
        raise BindingConflict("AX node has no complete geometry")
    for key, value in geometry.items():
        if type(value) not in (int, float) or not math.isfinite(value) or (key in ("width", "height") and value <= 0):
            raise BindingConflict("AX node geometry is invalid")
    return {"role": node["role"], "identifier": node.get("identifier", ""), "geometry": geometry}


def _visible(node, observation):
    bounds, window = _anchor(node)["geometry"], observation.get("geometry", {})
    if (not all(type(window.get(key)) in (int, float) and math.isfinite(window[key]) for key in ("x", "y", "width", "height"))
            or bounds["x"] < window["x"] or bounds["y"] < window["y"]
            or bounds["x"] + bounds["width"] > window["x"] + window["width"]
            or bounds["y"] + bounds["height"] > window["y"] + window["height"]
            or node.get("visible") is False):
        raise BindingConflict("source evidence is outside the selected window")
    return node


def _nodes(response):
    if type(response) is not dict or response.get("ok") is not True:
        raise BindingConflict("native observation was refused")
    result = response.get("result", {})
    observation, ax = result.get("observation"), result.get("ax", {})
    if (type(observation) is not dict or ax.get("available") is not True or ax.get("truncated") is not False
            or type(ax.get("nodes")) is not list or len(ax["nodes"]) > MAX_AX_NODES):
        raise BindingConflict("AX observation is unavailable or incomplete")
    nodes = ax["nodes"]
    for entry in nodes:
        if type(entry) is not dict or any(type(entry.get(key)) is str and len(entry[key].encode("utf-8")) > MAX_TEXT_BYTES for key in ("role", "identifier", "label", "title", "description", "value")):
            raise BindingConflict("AX observation contains unbounded nodes")
    return observation, nodes


def _provenance(source, config_digest, item):
    return {"app": source.scope["app"], "account": source.scope["account"],
            "conversation": source.scope["conversation"], "source_item": item["item_id"],
            "filename": item["filename"], "item_selector": item["item_selector"], "recipe_digest": config_digest}


class DesktopAttachmentRecipe:
    def __init__(self, store, coordinator, native, *, executor=None):
        self.store, self.coordinator, self.native = store, coordinator, native
        self.executor = executor or DesktopExecutor(store, coordinator, native)
        with store.transaction() as unit:
            unit.connection.execute("""CREATE TABLE IF NOT EXISTS desktop_retrieval_evidence (
                retrieval_id TEXT PRIMARY KEY REFERENCES authority_retrievals(retrieval_id),
                run_id TEXT NOT NULL REFERENCES authority_runs(run_id), recipe_digest TEXT NOT NULL,
                operation_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL, params_json TEXT NOT NULL,
                baseline_json TEXT, completion_anchor_json TEXT, observation_json TEXT,
                correlated_file_json TEXT)""")

    async def _check(self, context, phase):
        await self.coordinator.checkpoint(context, phase)
        with self.store.transaction() as unit:
            self.coordinator.validate_local(unit, context, phase, now=time.time())

    async def _config(self, context, source):
        root, config, digest = _recipe(source)
        if source != await self.coordinator.source(context):
            raise BindingConflict("recipe differs from the server-stored source")
        await self._check(context, "desktop-recipe-read")
        return root, config, digest

    @staticmethod
    def _identities(response, config):
        observation, nodes = _nodes(response)
        window = response.get("result", {}).get("window", {})
        if window.get("title") != config["window_title"] or window.get("pid") != observation.get("pid") or window.get("window_id") != observation.get("window_id"):
            raise BindingConflict("observed window differs from the selected title/identity")
        _visible(_unique(nodes, config["account_selector"]), observation)
        _visible(_unique(nodes, config["conversation_selector"]), observation)
        return observation, nodes

    async def _observe(self, context, config):
        await self._check(context, "desktop-app-read")
        response = await self.native.call("apps")
        selector = config["app_selector"]
        apps = response.get("result", {}).get("apps", []) if response.get("ok") is True else []
        found = [app for app in apps if app.get(selector["field"]) == selector["equals"]]
        if len(found) != 1 or type(found[0].get("pid")) is not int or found[0]["pid"] <= 0 or not found[0].get("process_start"):
            raise BindingConflict("selected application is unavailable or ambiguous")
        app = found[0]
        await self._check(context, "desktop-window-read")
        response = await self.native.call("windows", pid=app["pid"])
        windows = response.get("result", {}).get("windows", []) if response.get("ok") is True else []
        found = [window for window in windows if window.get("pid") == app["pid"] and window.get("title") == config["window_title"]]
        if len(found) != 1 or type(found[0].get("window_id")) is not int or found[0]["window_id"] <= 0:
            raise BindingConflict("selected window is unavailable or ambiguous")
        window = found[0]
        await self._check(context, "desktop-source-observe")
        response = await self.native.call("observe", pid=app["pid"], window_id=window["window_id"], include_ax=True)
        observation, _ = self._identities(response, config)
        if (observation.get("pid"), observation.get("process_start"), observation.get("window_id")) != (app["pid"], app["process_start"], window["window_id"]):
            raise BindingConflict("application/window identity changed during observation")
        await self._check(context, "desktop-source-observed")
        return response

    async def enumerate(self, context, source):
        _, config, digest = await self._config(context, source)
        response = await self._observe(context, config)
        observation, nodes = self._identities(response, config)
        result = []
        for item in config["items"]:
            match = _unique(nodes, item["item_selector"], optional=True)
            if match is not None:
                _visible(match, observation)
                result.append(SourceItem(item["item_id"], canonical_json(_provenance(source, digest, item))))
        return result

    @staticmethod
    def _item(config, digest, source, observed):
        found = [item for item in config["items"] if item["item_id"] == observed.item_id]
        if len(found) != 1 or observed.provenance != _provenance(source, digest, found[0]):
            raise BindingConflict("item does not match the original observed recipe provenance")
        return found[0]

    def _record(self, context, source, retrieval_id):
        with self.store._read_unit() as unit:
            row = CollectionRepository(unit)._exact(context, source, retrieval_id)
            evidence = unit.connection.execute("SELECT * FROM desktop_retrieval_evidence WHERE retrieval_id=?", (retrieval_id,)).fetchone()
            return row, dict(evidence) if evidence else None

    @staticmethod
    def _file(root, filename, limit):
        parent = descriptor = None
        try:
            parent = _directory(root)
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                return {"invalid": True}
            digest, count = hashlib.sha256(), 0
            while data := os.read(descriptor, min(1024 * 1024, limit + 1 - count)):
                count += len(data)
                if count > limit:
                    return {"invalid": True}
                digest.update(data)
            after = os.fstat(descriptor)
            if _identity(before) != _identity(after) or count != before.st_size:
                return {"invalid": True}
            return {"identity": _identity(before), "sha256": digest.hexdigest()}
        except FileNotFoundError:
            return None
        except OSError:
            return {"invalid": True}
        finally:
            for fd in (descriptor, parent):
                if fd is not None:
                    os.close(fd)

    async def retrieve(self, context, source, retrieval_id, observed):
        root, config, digest = await self._config(context, source)
        item = self._item(config, digest, source, observed)
        row, evidence = self._record(context, source, retrieval_id)
        if row["item_id"] != observed.item_id or row["item_provenance_json"] != observed.provenance_json:
            raise BindingConflict("retrieval differs from the original observed item")
        if row["operation_id"] is not None or row["state"] != "Prepared":
            return await self.correlate(context, source, retrieval_id, root / item["filename"])
        response = await self._observe(context, config)
        observation, nodes = self._identities(response, config)
        _visible(_unique(nodes, item["item_selector"]), observation)
        refs = observation.get("selectors", {})
        if refs.get("schema") != "secretary-selectors-v1" or refs.get("truncated") is not False or type(refs.get("refs")) is not list:
            raise BindingConflict("download selectors are unavailable or incomplete")
        action = _unique(refs["refs"], item["download_selector"])
        if not all(action.get(key) is True for key in ("enabled", "visible", "actionable")) or not action.get("ref"):
            raise BindingConflict("download control is not an enabled visible action")
        params = {"expected": observation, "element_ref": action["ref"]}
        operation_id = str(uuid.uuid4())
        await self._check(context, "desktop-retrieval-prepare")
        with self.store.transaction() as unit:
            self.coordinator.validate_local(unit, context, "desktop-retrieval-prepare", now=time.time())
            original = CollectionRepository(unit)._exact(context, source, retrieval_id)
            if original["operation_id"] is not None or original["state"] != "Prepared":
                raise BindingConflict("retrieval has already claimed an operation")
            existing = unit.connection.execute("SELECT * FROM desktop_retrieval_evidence WHERE retrieval_id=?", (retrieval_id,)).fetchone()
            if existing:
                if existing["run_id"] != context.run_id:
                    unit.connection.execute("DELETE FROM desktop_retrieval_evidence WHERE retrieval_id=?", (retrieval_id,))
                else:
                    operation_id, params = existing["operation_id"], json.loads(existing["params_json"])
            if not existing or existing["run_id"] != context.run_id:
                unit.connection.execute("""INSERT INTO desktop_retrieval_evidence
                    (retrieval_id,run_id,recipe_digest,operation_id,path,params_json) VALUES(?,?,?,?,?,?)""",
                    (retrieval_id, context.run_id, digest, operation_id, str(root / item["filename"]), canonical_json(params)))

        def bind(unit, owned, operation, attempt):
            CollectionRepository(unit).bind_operation(owned, source, retrieval_id, operation)

        async def before_effect(owned, fresh_response):
            current, current_nodes = self._identities(fresh_response, config)
            _visible(_unique(current_nodes, item["item_selector"]), current)
            before = _visible(_unique(current_nodes, item["completion"]["before"]), current)
            if _unique(current_nodes, item["completion"]["after"], optional=True) is not None:
                raise BindingConflict("completion changed before the download input")
            baseline = self._file(root, item["filename"], source.scope.get("max_bytes", 64 * 1024 * 1024))
            if baseline and baseline.get("invalid"):
                raise BindingConflict("candidate baseline is not a bounded regular file")
            with self.store.transaction() as unit:
                self.coordinator.validate_local(unit, owned, "desktop-download-baseline", now=time.time())
                bound = CollectionRepository(unit)._exact(owned, source, retrieval_id)
                if bound["operation_id"] != operation_id or bound["state"] != "Applying":
                    raise BindingConflict("baseline does not bind the claimed retrieval")
                unit.connection.execute("""UPDATE desktop_retrieval_evidence SET baseline_json=?,
                    completion_anchor_json=?,observation_json=? WHERE retrieval_id=? AND operation_id=? AND run_id=?""",
                    (canonical_json(baseline), canonical_json(_anchor(before)), canonical_json(current),
                     retrieval_id, operation_id, context.run_id))

        spec = make_effect_spec(context.task_id, "click", params, kind="collection_download", intent=observed.provenance)
        receipt = await self.executor.dispatch(context, operation_id, spec, "click", params, on_claim=bind, before_effect=before_effect)
        if receipt.get("state") == "Failed" and receipt.get("result", {}).get("no_effect") is True:
            with self.store.transaction() as unit:
                CollectionRepository(unit).confirm_no_effect(context, source, retrieval_id, operation_id, receipt["result"])
            return FileCandidate(root / item["filename"], observed.item_id, canonical_json({**observed.provenance,
                "run_id": context.run_id, "retrieval_id": retrieval_id, "operation_id": operation_id}), False, False)
        return await self.correlate(context, source, retrieval_id, root / item["filename"])

    async def correlate(self, context, source, retrieval_id, path):
        root, config, digest = await self._config(context, source)
        row, evidence = self._record(context, source, retrieval_id)
        observed = SourceItem(row["item_id"], row["item_provenance_json"])
        item = self._item(config, digest, source, observed)
        if (evidence is None or evidence["run_id"] != context.run_id or evidence["recipe_digest"] != digest
                or row["operation_id"] != evidence["operation_id"] or row["state"] not in ("Applying", "Unknown", "Verified")
                or evidence["baseline_json"] is None or evidence["observation_json"] is None):
            raise BindingConflict("retrieval lacks its original pre-effect baseline and operation")
        if Path(path) != root / item["filename"] or str(Path(path)) != evidence["path"]:
            raise BindingConflict("candidate path differs from the original retrieval filename")
        with self.store._read_unit() as unit:
            operation = CollectionRepository(unit)._operation(context, row["operation_id"])
            if operation["state"] not in ("Applying", "Unknown", "Applied"):
                raise BindingConflict("original operation cannot be correlated")
        response = await self._observe(context, config)
        current, nodes = self._identities(response, config)
        original = json.loads(evidence["observation_json"])
        if any(current.get(key) != original.get(key) for key in ("pid", "process_start", "window_id", "backend")):
            raise BindingConflict("original download application identity changed")
        _visible(_unique(nodes, item["item_selector"]), current)
        after = _unique(nodes, item["completion"]["after"], optional=True)
        complete = (after is not None and _anchor(after) == json.loads(evidence["completion_anchor_json"])
                    and _unique(nodes, item["completion"]["before"], optional=True) is None)
        current_file = self._file(root, item["filename"], source.scope.get("max_bytes", 64 * 1024 * 1024))
        baseline = json.loads(evidence["baseline_json"])
        correlated = (current_file is not None and not current_file.get("invalid")
                      and (baseline is None or current_file["identity"] != baseline["identity"]))
        if current_file and not current_file.get("invalid"):
            if "expected_size" in item and current_file["identity"]["size"] != item["expected_size"]:
                complete = False
            if "expected_sha256" in item and current_file["sha256"] != item["expected_sha256"]:
                complete = False
        await self._check(context, "desktop-file-correlated")
        with self.store.transaction() as unit:
            self.coordinator.validate_local(unit, context, "desktop-file-correlated", now=time.time())
            bound = CollectionRepository(unit)._exact(context, source, retrieval_id)
            if bound["operation_id"] != evidence["operation_id"]:
                raise BindingConflict("retrieval operation changed during correlation")
            unit.connection.execute("UPDATE desktop_retrieval_evidence SET correlated_file_json=? WHERE retrieval_id=?",
                                   (canonical_json(current_file) if correlated and complete else None, retrieval_id))
        return FileCandidate(path, observed.item_id, canonical_json({**observed.provenance,
            "run_id": context.run_id, "retrieval_id": retrieval_id, "operation_id": row["operation_id"]}), bool(complete), bool(correlated),
            observed_identity_json=canonical_json(current_file["identity"]) if correlated else None,
            expected_sha256=current_file["sha256"] if correlated else None)
