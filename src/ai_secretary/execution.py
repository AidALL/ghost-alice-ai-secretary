"""Bounded desktop effects under one run, attempt and retained OS owner."""

import asyncio
from dataclasses import replace
import math
import json
import time
import uuid

from .coordinator import AuthorityRepository
from .contracts import BindingConflict, EffectSpec, canonical_json
from .desktop_owner import DesktopOwnership
from .ledger import LedgerTx, _boot_id
from .native import EFFECT_COMMANDS


def make_effect_spec(task_id, command, params, *, kind=None, intent=None, outbound=None):
    """Bind concrete input and stable selector descriptors, not receipt tokens."""
    if command not in EFFECT_COMMANDS or kind not in (None, command, "collection_download"):
        raise ValueError("unsupported concrete effect descriptor")
    expected = params.get("expected")
    if not isinstance(expected, dict):
        raise ValueError("effect requires an observed target")
    target = {name: expected.get(name) for name in ("pid", "process_start", "window_id")}
    semantic = {name: value for name, value in params.items() if name not in ("expected", "element_ref")}
    if "element_ref" in params:
        selectors = expected.get("selectors", {})
        selected = [entry for entry in selectors.get("refs", []) if entry.get("ref") == params["element_ref"]]
        if len(selected) != 1:
            raise BindingConflict("selector reference must identify one observed descriptor")
        semantic["selector"] = {name: value for name, value in selected[0].items() if name != "ref"}
    return EffectSpec(task_id, kind or command, target, {"dispatch": {"command": command, "parameters": semantic}, "intent": intent, "outbound": outbound})


def _same_target(previous, current):
    for name in ("pid", "process_start", "window_id", "coordinate_space", "backend"):
        if previous.get(name) != current.get(name):
            raise ValueError("target identity changed while refreshing the dispatch observation")
    a, b = previous.get("geometry", {}), current.get("geometry", {})
    for name in ("x", "y", "width", "height"):
        if (type(a.get(name)) not in (int, float) or type(b.get(name)) not in (int, float)
                or not math.isfinite(a[name]) or not math.isfinite(b[name]) or abs(a[name] - b[name]) >= 0.5):
            raise ValueError("target geometry changed while refreshing the dispatch observation")


def _refresh_selector(params, previous, current):
    reference = params.get("element_ref")
    if reference is None:
        return
    old, new = previous.get("selectors", {}), current.get("selectors", {})
    if old.get("schema") != "secretary-selectors-v1" or new.get("schema") != old["schema"] or old.get("truncated") is not False or new.get("truncated") is not False:
        raise ValueError("selector observation is incomplete")
    selected = [entry for entry in old.get("refs", []) if entry.get("ref") == reference]
    if len(selected) != 1:
        raise ValueError("selector reference is missing or ambiguous")
    descriptor = {k: v for k, v in selected[0].items() if k != "ref"}
    matching = [entry for entry in new.get("refs", []) if {k: v for k, v in entry.items() if k != "ref"} == descriptor]
    if len(matching) != 1:
        raise ValueError("selector changed or became ambiguous")
    params["element_ref"] = matching[0]["ref"]


class DesktopExecutor:
    def __init__(self, store, coordinator, native):
        self.store, self.coordinator, self.native = store, coordinator, native

    async def dispatch(self, context, operation_id, spec, command, params, *, on_claim=None, before_effect=None, outbound=None):
        if spec.task_id != context.task_id:
            raise PermissionError("effect does not belong to the server-bound task")
        # Snapshot before the first await: nested modifier/selector aliases must
        # not alter the concrete descriptor after its immutable ledger binding.
        params = json.loads(canonical_json(params))
        outbound = json.loads(canonical_json(outbound))
        previous = params.get("expected")
        if not isinstance(previous, dict) or not all(name in previous for name in ("pid", "process_start", "window_id", "geometry", "coordinate_space", "backend")):
            raise ValueError("effect requires an observed target")
        payload = spec.payload
        if spec.kind not in (command, "collection_download"):
            raise BindingConflict("concrete command does not match the immutable operation kind")
        resolved = make_effect_spec(context.task_id, command, params, kind=spec.kind, intent=payload.get("intent") if isinstance(payload, dict) else None, outbound=outbound)
        if resolved.canonical != spec.canonical:
            raise BindingConflict("concrete dispatch does not match the immutable operation effect")
        await self.coordinator.checkpoint(context, "prepare-effect")
        now = time.time()
        with self.store.transaction() as uow:
            self.coordinator.validate_local(uow, context, "prepare-effect", now=now)
            ledger = LedgerTx(uow)
            prepared = ledger.prepare(operation_id, spec)
            if prepared["state"] in ("Applied", "Applying", "Unknown"):
                return ledger.claim(operation_id)
        async with DesktopOwnership(self.store.path) as owner:
            # A lock wait invalidates the previous gate sample. No DB lease or
            # Applying attempt is allocated until this new check passes.
            await self.coordinator.checkpoint(context, "claim-effect")
            boot, monotonic, wall = _boot_id(), time.monotonic(), time.time()
            lease_owner = str(uuid.uuid4())
            with self.store.transaction() as uow:
                self.coordinator.validate_local(uow, context, "claim-effect", now=wall)
                ledger = LedgerTx(uow)
                if outbound:
                    ledger.validate_approval(outbound["draft_id"], outbound["content"], task_id=context.task_id)
                # Replayed effects must not consume a new lease or overwrite the
                # binding to an older Applying/Unknown attempt.
                claim = ledger.claim(operation_id)
                if claim["state"] != "claimed":
                    return claim
                fence = ledger.acquire_lease(lease_owner, ttl=2 * self.native.timeout + 10, boot_id=boot, now=monotonic)
                attempt = claim["attempt_id"]
                uow.connection.execute("INSERT OR REPLACE INTO execution_operations VALUES(?,?,?)", (operation_id, context.run_id, attempt))
                uow.connection.execute("UPDATE execution_contexts SET desktop_fence=? WHERE run_id=?", (fence, context.run_id))
                owned_context = replace(context, desktop_fence=fence)
                if on_claim:
                    on_claim(uow, owned_context, operation_id, attempt)
            payload = {name: getattr(owned_context, name) for name in ("run_id", "session", "admission_id", "task_id", "input_event_id", "lifecycle_id", "epoch", "run_fence")}
            payload.update(db_path=str(self.store.path), operation_id=operation_id, attempt_id=attempt, lease_owner=lease_owner, lease_fence=fence, lease_boot_id=boot, dispatch_lock_fd=owner.fd)
            effect_requested = False
            try:
                await self.coordinator.checkpoint(owned_context, "observe-for-effect")
                observed = await self.native.call_authorized("observe", payload, owner, pid=previous["pid"], window_id=previous["window_id"], include_ax=True)
                if observed.get("ok") is not True:
                    raise ValueError("fresh target observation was refused")
                current = observed["result"]["observation"]
                _same_target(previous, current)
                _refresh_selector(params, previous, current)
                params["expected"] = current
                if before_effect:
                    await before_effect(owned_context, observed)
                await self.coordinator.checkpoint(owned_context, "dispatch-effect")
                wall, monotonic = time.time(), time.monotonic()
                with self.store.transaction() as uow:
                    self.coordinator.validate_local(uow, owned_context, "dispatch-effect", now=wall)
                    ledger = LedgerTx(uow)
                    ledger.assert_lease(lease_owner, fence, boot_id=boot, now=monotonic)
                    if outbound:
                        ledger.validate_approval(outbound["draft_id"], outbound["content"], task_id=context.task_id)
                effect_requested = True
                result = await self.native.call_authorized(command, payload, owner, **params)
                if spec.kind == "collection_download" and result.get("effect_status") != "not_started":
                    status = "Unknown"  # Await source correlation and verified file.
                elif result.get("effect_status") == "dispatched" and result.get("ok") is True:
                    status = "Applied"  # OS dispatch, never application completion.
                elif result.get("effect_status") == "not_started":
                    status, result = "Failed", {**result, "no_effect": True}
                else:
                    status = "Unknown"
                with self.store.transaction() as uow:
                    LedgerTx(uow).finish(operation_id, attempt, status, result)
                return {"state": status, "operation_id": operation_id, "result": result}
            except asyncio.CancelledError:
                with self.store.transaction() as uow:
                    LedgerTx(uow).finish(operation_id, attempt, "Unknown" if effect_requested else "Failed", {"error": "cancelled after child cleanup", "no_effect": not effect_requested})
                raise
            except Exception as error:
                no_effect = not effect_requested or getattr(error, "effect_uncertain", True) is False
                status = "Failed" if no_effect else "Unknown"
                result = {"error": str(error), "no_effect": no_effect}
                with self.store.transaction() as uow:
                    LedgerTx(uow).finish(operation_id, attempt, status, result)
                return {"state": status, "operation_id": operation_id, "result": result}
            finally:
                # NativeClient returns/raises only after cleanup or with its
                # owner pinned. The latter makes owner.release refuse below.
                with self.store.transaction() as uow:
                    LedgerTx(uow).release_lease(lease_owner, fence, boot_id=boot)
                    uow.connection.execute("UPDATE execution_contexts SET desktop_fence=? WHERE run_id=? AND desktop_fence=?", (context.desktop_fence, context.run_id, fence))
