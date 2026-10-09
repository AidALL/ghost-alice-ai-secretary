from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from ai_secretary.jobs import RunnerConfig, SchedulerProjection


class OpenClawSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            from ai_secretary.adapters.openclaw_scheduler import OpenClawScheduler
        except ImportError as error:
            self.fail(f"replaceable OpenClaw scheduler is missing: {error}")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "openclaw"
        self.state.mkdir()
        self.config = self.state / "openclaw.json"
        self.config.write_text('{"cron":{"failureAlert":false}}')
        self.log = self.state / "argv.jsonl"
        self.executable = self.root / "openclaw-cli"
        self.executable.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
root = pathlib.Path(os.environ['OPENCLAW_STATE_DIR'])
argv = sys.argv[1:]
with (root / 'argv.jsonl').open('a') as log:
    log.write(json.dumps({'argv': argv, 'config': os.environ.get('OPENCLAW_CONFIG_PATH'),
                         'provider_key': os.environ.get('ANTHROPIC_API_KEY')}) + '\\n')
database = root / 'jobs.json'
jobs = json.loads(database.read_text()) if database.exists() else []
if argv[:2] == ['cron', 'add']:
    def value(flag): return argv[argv.index(flag) + 1]
    key = value('--declaration-key')
    old = next((job for job in jobs if job['declarationKey'] == key), None)
    job = old or {'id': 'upstream-1', 'declarationKey': key}
    job.update({'enabled': '--disabled' not in argv, 'schedule': {'kind': 'at', 'at': value('--at')},
                'payload': {'kind': 'command', 'argv': json.loads(value('--command-argv')),
                            'timeoutSeconds': int(value('--timeout-seconds'))},
                'sessionTarget': value('--session'), 'delivery': {'mode': 'none'}})
    if not old: jobs.append(job)
    database.write_text(json.dumps(jobs))
    print(json.dumps({'created': not bool(old), **({'updated': False} if old else {}),
                      'job': job, 'deliveryPreview': {'label': 'not requested', 'detail': 'not requested'}}))
elif argv[:2] == ['cron', 'edit']:
    job = next(job for job in jobs if job['id'] == argv[2])
    job.update({'failureAlert': False, 'enabled': True})
    database.write_text(json.dumps(jobs))
    print(json.dumps(job))
elif argv[:2] == ['cron', 'list']:
    print(json.dumps({'jobs': jobs, 'total': len(jobs), 'snapshotRevision': 'sha256:test-snapshot',
                      'offset': 0, 'limit': 200, 'hasMore': False, 'nextOffset': None,
                      'deliveryPreviews': {}}))
elif argv[:2] == ['cron', 'rm']:
    removed = any(job['id'] == argv[2] for job in jobs)
    if not removed: raise SystemExit('unknown automation id')
    database.write_text(json.dumps([job for job in jobs if job['id'] != argv[2]]))
    print(json.dumps({'ok': True, 'removed': removed}))
else:
    raise SystemExit('unsupported test executable command')
''')
        self.executable.chmod(0o700)
        self.adapter = OpenClawScheduler(str(self.executable), str(self.state), str(self.config), timeout_seconds=3)
        self.runner = RunnerConfig(str(Path(sys.executable).resolve()), str(self.root / "secretary"), 30)
        self.projection = SchedulerProjection("secretary:job:1:1:occurrence", "occurrence", "job", 1, "scheduler", 1, 1800000000.123456)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    async def test_declaration_keyed_add_envelope_finalizes_nested_job(self):
        receipt = await self.adapter.create(self.projection, self.runner)
        self.assertEqual(receipt.upstream_id, "upstream-1")
        self.assertTrue(receipt.enabled and receipt.failure_alert_disabled)
        self.assertEqual([call["argv"][:2] for call in self.calls()],
                         [["cron", "add"], ["cron", "edit"]])

    async def test_converged_add_envelope_uses_same_nested_job(self):
        original = await self.adapter.create(self.projection, self.runner)
        converged = await self.adapter.create(self.projection, self.runner)
        self.assertEqual(original, converged)
        self.assertEqual(len(json.loads((self.state / "jobs.json").read_text())), 1)

    async def test_keyed_add_rejects_bare_job_without_envelope(self):
        original_call = self.adapter._call
        async def bare_job(*args):
            result = await original_call(*args)
            return result["job"] if args[:2] == ("cron", "add") else result
        self.adapter._call = bare_job
        with self.assertRaises(ValueError):
            await self.adapter.create(self.projection, self.runner)
        self.assertEqual([call["argv"][:2] for call in self.calls()], [["cron", "add"]])

    async def test_missing_timer_remove_is_unconfirmed_cli_error(self):
        with self.assertRaises(RuntimeError):
            await self.adapter.remove("missing")

    async def test_list_accepts_complete_cli_inventory_larger_than_one_gateway_page(self):
        receipt = await self.adapter.create(self.projection, self.runner)
        owned = json.loads((self.state / "jobs.json").read_text())
        outside = [{"id": f"other-{number}", "declarationKey": f"outside:{number}"} for number in range(204)]
        (self.state / "jobs.json").write_text(json.dumps(outside + owned))
        self.assertEqual(await self.adapter.list(), (receipt,))

    async def test_list_rejects_incomplete_or_inconsistent_inventory(self):
        original_call = self.adapter._call
        complete = await original_call("cron", "list", "--all", "--json")
        invalid = ({"hasMore": True, "nextOffset": 200}, {"total": 1}, {"total": True},
                   {"offset": 200}, {"snapshotRevision": ""}, {"limit": 10001})
        for patch in invalid:
            with self.subTest(patch=patch):
                async def incomplete(*args):
                    return {**complete, **patch}
                self.adapter._call = incomplete
                with self.assertRaises(ValueError):
                    await self.adapter.list()

    async def test_actual_cli_argv_is_fixed_command_with_no_delivery_or_model(self):
        receipt = await self.adapter.create(self.projection, self.runner)
        calls = self.calls()
        added, edited = calls[0]["argv"], calls[1]["argv"]
        self.assertEqual(added[:2], ["cron", "add"])
        self.assertIn("--disabled", added)
        self.assertIn("--no-deliver", added)
        self.assertEqual(added[added.index("--session") + 1], "isolated")
        self.assertEqual(json.loads(added[added.index("--command-argv") + 1]), list(self.runner.command("occurrence")))
        self.assertEqual(added[added.index("--timeout-seconds") + 1], "30")
        self.assertEqual(edited, ["cron", "edit", "upstream-1", "--no-failure-alert", "--enable", "--json"])
        for flag in ("--model", "--message", "--channel", "--to", "--script", "--command"):
            self.assertNotIn(flag, added)
        self.assertTrue(receipt.matches(self.projection, self.runner))
        self.assertTrue(receipt.failure_alert_disabled)

    async def test_list_and_remove_roundtrip_cli_json(self):
        created = await self.adapter.create(self.projection, self.runner)
        self.assertEqual(await self.adapter.list(), (created,))
        await self.adapter.remove(created.upstream_id)
        self.assertEqual(await self.adapter.list(), ())
        self.assertIn(["cron", "list", "--all", "--json"], [call["argv"] for call in self.calls()])
        self.assertIn(["cron", "rm", "upstream-1", "--json"], [call["argv"] for call in self.calls()])

    async def test_finalize_interrupted_disabled_add_edits_exact_id_without_add(self):
        self.assertTrue(callable(getattr(self.adapter, "finalize", None)), "adapter needs exact-ID finalization")
        created = await self.adapter.create(self.projection, self.runner)
        data = json.loads((self.state / "jobs.json").read_text())
        data[0].update(enabled=False, failureAlert=None)
        (self.state / "jobs.json").write_text(json.dumps(data))
        disabled = (await self.adapter.list())[0]
        before = len(self.calls())
        enabled = await self.adapter.finalize(self.projection, disabled, self.runner)
        self.assertEqual(enabled.upstream_id, created.upstream_id)
        self.assertTrue(enabled.enabled and enabled.failure_alert_disabled)
        self.assertEqual([call["argv"] for call in self.calls()[before:]],
                         [["cron", "edit", "upstream-1", "--no-failure-alert", "--enable", "--json"]])

    async def test_isolated_state_config_and_no_inherited_provider_credentials(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-provider-key", "OPENCLAW_STATE_DIR": "/wrong"}):
            await self.adapter.create(self.projection, self.runner)
        self.assertEqual(self.calls()[0]["config"], str(self.config))
        self.assertIsNone(self.calls()[0]["provider_key"])

    async def test_explicit_isolation_root_accepts_installed_sibling_configuration(self):
        from ai_secretary.adapters.openclaw_scheduler import OpenClawScheduler
        config = self.root / "openclaw.json"
        original = b'{"mutable":"existing operator configuration"}\n'
        config.write_bytes(original)
        try:
            adapter = OpenClawScheduler(str(self.executable), str(self.state), str(config),
                                        isolation_root=str(self.root), timeout_seconds=3)
        except (TypeError, ValueError) as error:
            self.fail(f"installed state/config layout must fit its explicit isolation root: {error}")
        await adapter.create(self.projection, self.runner)
        self.assertEqual(adapter.state_dir, str(self.state))
        self.assertEqual(adapter.config_path, str(config))
        self.assertEqual(self.calls()[0]["config"], str(config))
        self.assertEqual(config.read_bytes(), original)

    async def test_explicit_isolation_root_rejects_outside_state_or_configuration(self):
        from ai_secretary.adapters.openclaw_scheduler import OpenClawScheduler
        for state, config, root in (
                (self.root / "outside", self.config, self.state),
                (self.state, self.root / "outside.json", self.state),
                (self.state, self.config, Path("relative"))):
            with self.subTest(state=state, config=config, root=root), self.assertRaises(ValueError):
                OpenClawScheduler(str(self.executable), str(state), str(config), isolation_root=str(root))

    async def test_adapter_rejects_arbitrary_mapping_and_malformed_receipt(self):
        with self.assertRaises(TypeError):
            await self.adapter.create({"argv": ["sh", "-c", "true"]}, self.runner)
        await self.adapter.create(self.projection, self.runner)
        data = json.loads((self.state / "jobs.json").read_text())
        data[0]["payload"]["kind"] = "agentTurn"
        (self.state / "jobs.json").write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            await self.adapter.list()

    async def test_scheduler_timeout_reaps_client_and_is_unknown(self):
        self.executable.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(20)\n")
        self.adapter.timeout_seconds = .05
        with self.assertRaises(TimeoutError):
            await self.adapter.list()

    async def test_paths_and_timeout_are_bounded(self):
        from ai_secretary.adapters.openclaw_scheduler import OpenClawScheduler
        for args in (("relative", str(self.state), str(self.config)),
                     (str(self.executable), "relative", str(self.config)),
                     (str(self.executable), str(self.state), str(self.root / "outside.json"))):
            with self.assertRaises(ValueError):
                OpenClawScheduler(*args)
        with self.assertRaises(ValueError):
            replace(self.runner, timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
