"""Pinned OpenClaw 2026.9.8 cron CLI projection, without core authority IO.

The CLI's add command lacks a failure-alert flag. Create disabled, then set
failureAlert=false and enable with edit. Any interrupted operation is uncertain
and the core reconciles its declaration key from list --all.
"""

import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

from ..contracts import identifier
from ..jobs import RunnerConfig, SchedulerProjection, SchedulerReceipt


def _absolute(value, label):
    if type(value) is not str or not Path(value).is_absolute() or ".." in Path(value).parts or "\x00" in value:
        raise ValueError(f"{label} must be a canonical absolute path")
    return Path(value)


class OpenClawScheduler:
    def __init__(self, executable, state_dir, config_path, *, isolation_root=None, timeout_seconds=30):
        self.executable = str(_absolute(executable, "executable"))
        state = _absolute(state_dir, "state_dir")
        config = _absolute(config_path, "config_path")
        root = _absolute(state_dir if isolation_root is None else isolation_root, "isolation_root")
        for path in (state, config):
            if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("scheduler state and configuration must be inside the explicit isolation root")
        if config == root:
            raise ValueError("scheduler configuration must be a path inside the isolation root")
        self.state_dir = str(state)
        self.config_path = str(config)
        self.isolation_root = str(root)
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("scheduler CLI timeout must be bounded to 120 seconds")
        self.timeout_seconds = timeout_seconds

    async def _call(self, *args):
        # Only host paths/locale are inherited; provider credentials, user model
        # overrides, and unrelated OpenClaw profile selectors are not inherited.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
        env.update(OPENCLAW_STATE_DIR=self.state_dir, OPENCLAW_CONFIG_PATH=self.config_path,
                   OPENCLAW_NO_RESPAWN="1")
        process = await asyncio.create_subprocess_exec(self.executable, *args, env=env,
                                                       stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)

        async def read_bounded(stream):
            chunks, size = [], 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("scheduler CLI output exceeded its limit")
                chunks.append(chunk)
            return b"".join(chunks)

        readers = [asyncio.create_task(read_bounded(process.stdout)),
                   asyncio.create_task(read_bounded(process.stderr))]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
        except BaseException:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        if process.returncode != 0:
            # stderr can contain operator paths or credentials. The core stores
            # only the exception type and does not persist raw provider output.
            raise RuntimeError(f"scheduler CLI returned {process.returncode}")
        result = json.loads(stdout)
        if type(result) is not dict:
            raise ValueError("scheduler CLI must return a JSON object")
        return result

    @staticmethod
    def _receipt(job):
        if type(job) is not dict:
            raise ValueError("scheduler job must be an object")
        upstream_id = identifier(job.get("id"), "upstream_id")
        key = identifier(job.get("declarationKey"), "declaration_key")
        if not key.startswith("secretary:") or job.get("sessionTarget") != "isolated":
            raise ValueError("scheduler job is not an isolated Secretary declaration")
        payload, schedule = job.get("payload", {}), job.get("schedule", {})
        if payload.get("kind") != "command" or schedule.get("kind") != "at":
            raise ValueError("Secretary timers require one-shot command payloads")
        argv = payload.get("argv")
        if (type(argv) is not list or len(argv) != 7 or any(type(arg) is not str for arg in argv)
                or argv[1:4] != ["-m", "ai_secretary.schedule_runner", "--state"]
                or argv[5] != "--occurrence"):
            raise ValueError("scheduler command is not the fixed occurrence runner")
        _absolute(argv[0], "runner interpreter")
        _absolute(argv[4], "runner state")
        occurrence_id = identifier(argv[6], "occurrence_id")
        if payload.get("cwd") is not None or payload.get("env") not in (None, {}) or payload.get("input") is not None:
            raise ValueError("scheduler command has unowned execution overrides")
        timeout = payload.get("timeoutSeconds")
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("scheduler runner timeout is not bounded")
        when = schedule.get("at")
        if type(when) is not str:
            raise ValueError("scheduler at time must be a timezone-qualified ISO timestamp")
        parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("scheduler at time has no timezone")
        enabled = job.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("scheduler enabled state must be explicit")
        return SchedulerReceipt(upstream_id, key, occurrence_id, parsed.timestamp(), tuple(argv),
                                enabled, job.get("delivery", {}).get("mode") == "none",
                                job.get("failureAlert") is False)

    async def create(self, projection, runner_config):
        if not isinstance(projection, SchedulerProjection) or not isinstance(runner_config, RunnerConfig):
            raise TypeError("scheduler create requires immutable projection and runner configuration")
        expected_key = f"secretary:{projection.job_id}:{projection.job_revision}:{projection.epoch}:{projection.occurrence_id}"
        if projection.declaration_key != expected_key or projection.lifecycle_id != "scheduler":
            raise ValueError("projection declaration key does not bind its durable identity")
        when = datetime.fromtimestamp(projection.due_at, timezone.utc).isoformat(timespec="microseconds")
        result = await self._call("cron", "add", "--name", projection.declaration_key,
                                  "--declaration-key", projection.declaration_key,
                                  "--at", when, "--command-argv", json.dumps(runner_config.command(projection.occurrence_id)),
                                  "--timeout-seconds", str(runner_config.timeout_seconds),
                                  "--output-max-bytes", "65536", "--session", "isolated",
                                  "--no-deliver", "--disabled", "--keep-after-run", "--json")
        # Declaration-keyed cron.add has a mutation envelope in this pinned
        # Gateway. cron.update deliberately returns its job directly.
        if (type(result.get("created")) is not bool or type(result.get("job")) is not dict
                or ("updated" in result and type(result["updated"]) is not bool)
                or type(result.get("deliveryPreview")) is not dict):
            raise ValueError("keyed scheduler add requires its mutation envelope")
        receipt = self._receipt(result["job"])
        if not receipt.matches(projection, runner_config) or receipt.enabled or not receipt.delivery_none:
            raise ValueError("created timer does not match the disabled occurrence projection")
        return await self.finalize(projection, receipt, runner_config)

    async def finalize(self, projection, receipt, runner_config):
        """Activate one observed timer ID; this never invokes cron add."""
        if (not isinstance(projection, SchedulerProjection) or not isinstance(receipt, SchedulerReceipt)
                or not isinstance(runner_config, RunnerConfig)):
            raise TypeError("finalization requires immutable projection, receipt and runner configuration")
        expected_key = f"secretary:{projection.job_id}:{projection.job_revision}:{projection.epoch}:{projection.occurrence_id}"
        if (projection.lifecycle_id != "scheduler" or projection.declaration_key != expected_key
                or not receipt.matches(projection, runner_config) or receipt.upstream_id.startswith("-")):
            raise ValueError("timer identity does not bind the exact scheduled occurrence")
        result = await self._call("cron", "edit", receipt.upstream_id,
                                  "--no-failure-alert", "--enable", "--json")
        enabled = self._receipt(result)
        if (enabled.upstream_id != receipt.upstream_id or not enabled.matches(projection, runner_config)
                or not enabled.enabled or not enabled.delivery_none or not enabled.failure_alert_disabled):
            raise ValueError("enabled timer does not match its occurrence projection")
        return enabled

    async def list(self):
        result = await self._call("cron", "list", "--all", "--json")
        jobs = result.get("jobs")
        # The pinned CLI owns bounded pagination: 200 jobs/page, 50 pages,
        # one consistent snapshot, and at most three snapshot restarts. Its
        # JSON is the aggregated inventory, never an individual Gateway page.
        # Require that complete output before core code can infer absence.
        metadata = {"snapshotRevision", "total", "offset", "limit", "hasMore", "nextOffset"}
        if (type(jobs) is not list or len(jobs) > 10000 or not metadata <= result.keys()
                or type(result["snapshotRevision"]) is not str or not result["snapshotRevision"]
                or type(result["total"]) is not int or result["total"] != len(jobs)
                or type(result["offset"]) is not int or result["offset"] != 0
                or type(result["limit"]) is not int or not 1 <= result["limit"] <= 200
                or result["hasMore"] is not False or result["nextOffset"] is not None
                or any(type(job) is not dict for job in jobs)):
            raise ValueError("scheduler list response must be a complete bounded inventory")
        identities = [identifier(job.get("id"), "upstream_id") for job in jobs]
        if len(set(identities)) != len(identities):
            raise ValueError("scheduler list inventory repeats an upstream identity")
        return tuple(self._receipt(job) for job in jobs
                     if isinstance(job.get("declarationKey"), str)
                     and job["declarationKey"].startswith("secretary:"))

    async def remove(self, upstream_id):
        identifier(upstream_id, "upstream_id")
        if upstream_id.startswith("-") or "\x00" in upstream_id:
            raise ValueError("upstream id is not a CLI positional identity")
        result = await self._call("cron", "rm", upstream_id, "--json")
        if result.get("ok") is not True or type(result.get("removed")) is not bool:
            raise ValueError("scheduler removal is not confirmed")
