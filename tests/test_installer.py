"""Installer lifecycle checks use disposable filesystems, never home installs."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GHOST_ROOT = Path(os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice"))).expanduser().resolve()
SPEC = importlib.util.spec_from_file_location("secretary_installer", ROOT / "scripts/installer.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "source"
        skill = self.source / "addons/ai-secretary/skill"
        skill.mkdir(parents=True)
        (self.source / "README.md").write_text("Fixture installation guide.\n")
        (self.source / "README_ko.md").write_text("Fixture installation guide in Korean.\n")
        for name in ("LICENSE", "NOTICE", "ARCHITECTURE.md", "THIRD_PARTY_NOTICES.md"):
            (self.source / name).write_bytes((ROOT / name).read_bytes())
        (self.source / "pyproject.toml").write_text('[project]\nname = "ai-secretary-fixture"\nversion = "0.1.0"\n')
        (skill / "SKILL.md").write_text("---\nname: ai-secretary\ndescription: Test\n---\nTest skill.\n")
        (self.source / "addons-manifest.json").write_text(json.dumps({"manifest_version": 1, "addons": [{"id": "ai-secretary", "path": "addons/ai-secretary"}]}))
        (skill.parent / "addon.json").write_text(json.dumps({"addon_id": "ai-secretary", "addon_version": "0.1.0", "platforms": ["codex", "claude"], "skills": [{"name": "ai-secretary", "source": "skill", "skill_dir": "skill"}]}))
        (self.source / "src/ai_secretary").mkdir(parents=True)
        (self.source / "src/ai_secretary/__init__.py").write_text("")
        for name in ("control", "service"):
            (self.source / f"src/ai_secretary/{name}.py").write_text("print('fixture')\n")
        (self.source / "scripts").mkdir()
        (self.source / "scripts/installer.py").write_bytes((ROOT / "scripts/installer.py").read_bytes())
        build = self.source / "scripts/build_native.sh"
        build.write_text("#!/bin/sh\nset -eu\nmkdir -p build\nprintf '#!/bin/sh\\necho native-fixture\\n' > build/desktop-secretary\nchmod +x build/desktop-secretary\n")
        build.chmod(0o755)
        (self.source / "native").mkdir()
        (self.source / "native/Desktop.swift").write_text("// fixture\n")
        plugin = self.source / "openclaw-plugin"
        plugin.mkdir()
        for name, content in {"index.mjs": "export default {};\n", "package.json": '{"name":"ai-secretary"}', "openclaw.plugin.json": '{"id":"ai-secretary","configSchema":{"type":"object"}}'}.items():
            (plugin / name).write_text(content)
        resources = self.source / "src/ai_secretary/resources"
        (resources / "schemas").mkdir(parents=True)
        (resources / "schemas/desktop-tool-schemas.mjs").write_text("export default {canonical: true};\n")
        (resources / "ga-gate.mjs").write_text("export const canonicalGate = true;\n")
        self.runtime = self.base / "runtime"
        package = self.runtime / "node_modules/openclaw"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": "2026.9.8"}))
        (package / "openclaw.mjs").write_text("console.log('2026.9.8');\n")
        (self.runtime / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/openclaw": {"version": "2026.9.8", "integrity": installer.OPENCLAW_INTEGRITY}}}))
        self.node = self.base / "node"
        (self.node / "bin").mkdir(parents=True)
        binary = self.node / "bin/node"
        binary.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo v24.21.0; else echo 'OpenClaw 2026.9.8'; fi\n")
        binary.chmod(0o755)
        self.prefix = self.base / "prefix"
        self.skills = self.base / "skills"
        self.registry = self.base / "registry"
        self.subject = installer.Installer(self.prefix, [self.skills], self.source,
            GHOST_ROOT, registry_dir=self.registry,
            openclaw_source=self.runtime, node_source=self.node)
        # Lifecycle fixtures deliberately contain no real npm artifact. Package
        # integrity is exercised separately by the opt-in pinned runtime test.
        artifact_check = patch.object(self.subject, "_verify_cached_package")
        artifact_check.start()
        self.addCleanup(artifact_check.stop)

    def test_install_is_discoverable_in_the_actual_ghost_alice_registry(self):
        self.subject.install()
        records = self.subject.framework().registry.read_all(addons_dir=self.registry)
        self.assertEqual([record["addon_id"] for record in records], ["ai-secretary"])
        manifest = self.subject._manifest()
        self.assertEqual(manifest["registry_dir"], str(self.registry))
        resources = [entry for entry in manifest["resources"] if entry["role"] == "registry"]
        self.assertEqual([entry["path"] for entry in resources], [str(self.registry / "ai-secretary.json")])
        sidecar = records[0]
        self.assertEqual(sidecar["installation"]["prefix"], str(self.prefix))
        self.assertEqual(self.subject.install()["status"], "unchanged")

    def test_installed_public_package_carries_exact_license_and_notice(self):
        self.subject.install()
        entries = self.subject._manifest()["resources"][0]["entries"]
        for name in ("LICENSE", "NOTICE", "ARCHITECTURE.md", "THIRD_PARTY_NOTICES.md"):
            with self.subTest(document=name):
                installed = self.prefix / "managed/app" / name
                self.assertTrue(installed.is_file(), f"installed public package lacks {name}")
                self.assertEqual(installed.read_bytes(), (self.source / name).read_bytes())
                self.assertIn("app/" + name, entries)

    def test_paired_readmes_are_shipped_with_owned_runtime(self):
        self.subject.install()
        entries = self.subject._manifest()["resources"][0]["entries"]
        for name in ("README.md", "README_ko.md"):
            with self.subTest(document=name):
                installed = self.prefix / "managed/app" / name
                self.assertTrue(installed.is_file(), f"installed public package lacks {name}")
                self.assertEqual(installed.read_bytes(), (self.source / name).read_bytes())
                self.assertIn("app/" + name, entries)

    def test_korean_readme_source_change_refreshes_owned_package_and_preserves_state(self):
        self.subject.install()
        state_marker = self.prefix / "state/secretary/user-work.txt"
        state_marker.write_text("preserve this user state\n")
        configuration = self.prefix / "state/openclaw.json"
        config = json.loads(configuration.read_text())
        config["user-customization"] = "preserve"
        configuration.write_text(json.dumps(config))

        source_guide = self.source / "README_ko.md"
        source_guide.write_bytes(source_guide.read_bytes() + b"Updated fixture content.\n")

        self.assertEqual(self.subject.install()["status"], "installed")
        self.assertEqual((self.prefix / "managed/app/README_ko.md").read_bytes(), source_guide.read_bytes())
        self.assertEqual(state_marker.read_text(), "preserve this user state\n")
        self.assertEqual(json.loads(configuration.read_text())["user-customization"], "preserve")

    def test_notice_source_change_refreshes_the_owned_public_package(self):
        self.subject.install()
        notice = self.source / "NOTICE"
        notice.write_bytes(notice.read_bytes() + b"\nUpdated fixture attribution.\n")
        self.assertEqual(self.subject.install()["status"], "installed")
        self.assertEqual((self.prefix / "managed/app/NOTICE").read_bytes(), notice.read_bytes())

    def test_cli_resolves_ghost_root_from_configuration(self):
        from unittest.mock import MagicMock
        mocked = MagicMock()
        mocked.status.return_value = {"status": "not-installed"}
        configured = self.base / "configured-ghost-root"
        with patch.dict(os.environ, {"SECRETARY_GHOST_ROOT": str(configured)}):
            with patch.object(installer, "Installer", return_value=mocked) as constructor:
                with patch("builtins.print"):
                    self.assertEqual(installer.main(["status"]), 0)
        self.assertEqual(constructor.call_args.args[3], configured)

    def _select_two_platform_registries(self):
        self.platform_registries = {"codex": self.registry, "claude": self.base / "claude-registry"}
        self.subject = installer.Installer(self.prefix, [self.skills, self.base / "claude-skills"], self.source,
            GHOST_ROOT, platform_registries=self.platform_registries,
            openclaw_source=self.runtime, node_source=self.node)
        artifact_check = patch.object(self.subject, "_verify_cached_package")
        artifact_check.start()
        self.addCleanup(artifact_check.stop)
        return self.platform_registries

    def test_two_platform_records_are_installed_and_removed_with_one_bundle(self):
        selected = self._select_two_platform_registries()
        self.subject.install()
        registry = self.subject.framework().registry
        for platform_name, directory in selected.items():
            self.assertEqual(registry.read_all_ids(addons_dir=directory), ["ai-secretary"])
            record = registry.read_record("ai-secretary", addons_dir=directory)
            self.assertEqual(record["platform"], platform_name)
            self.assertEqual(record["installation"].get("platform_registries"), {key: str(value) for key, value in selected.items()})
        self.assertEqual(self.subject._manifest()["platform_registries"], {key: str(value) for key, value in selected.items()})
        self.assertEqual(self.subject.install()["status"], "unchanged")
        self.assertTrue((self.base / "claude-skills/ai-secretary/SKILL.md").is_file())
        self.assertEqual(self.subject.uninstall()["status"], "uninstalled")
        self.assertFalse((self.base / "claude-skills/ai-secretary").exists())
        for directory in selected.values():
            self.assertEqual(registry.read_all_ids(addons_dir=directory), [])

    def test_second_platform_publication_failure_rolls_back_the_entire_bundle(self):
        selected = self._select_two_platform_registries()
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def fail_second_record(source, dest):
            if Path(dest) == selected["claude"] / "ai-secretary.json" and Path(source).parent.name == ".ghost-alice-install-staging":
                raise OSError("injected second platform publication failure")
            return original(source, dest)
        with patch.object(framework.transaction, "_rename_path", side_effect=fail_second_record):
            with self.assertRaises(installer.InstallError):
                self.subject.install()
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse((self.skills / "ai-secretary").exists())
        self.assertFalse((self.base / "claude-skills/ai-secretary").exists())
        self.assertFalse(self.subject.manifest_path.exists())
        for directory in selected.values():
            self.assertFalse((directory / "ai-secretary.json").exists())

    def test_second_platform_record_drift_preserves_the_entire_bundle(self):
        selected = self._select_two_platform_registries()
        self.subject.install()
        record_path = selected["claude"] / "ai-secretary.json"
        self.assertTrue(record_path.is_file(), "second platform sidecar is missing")
        record = json.loads(record_path.read_text())
        record["installation"]["user_note"] = "keep attribution"
        record_path.write_text(json.dumps(record))
        before = self.subject.manifest_path.read_bytes()
        with self.assertRaises(installer.InstallError):
            self.subject.uninstall()
        self.assertEqual(self.subject.manifest_path.read_bytes(), before)
        for directory in selected.values():
            self.assertTrue((directory / "ai-secretary.json").is_file())
        self.assertTrue((self.prefix / "managed/bin/openclaw").is_file())

    def test_install_rejects_unsupported_desktop_os_before_any_write(self):
        with patch.object(installer.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(installer.InstallError, "macOS|Darwin"):
                self.subject.install()
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.subject.lock_path.exists())

    def test_invalid_typed_registry_cli_selections_fail_before_any_write(self):
        selections = [
            ["--platform-registry", "unknown=" + str(self.registry)],
            ["--platform-registry", "codex=" + str(self.registry), "--platform-registry", "codex=" + str(self.base / "other")],
            ["--platform-registry", "codex=" + str(self.registry), "--platform-registry", "claude=" + str(self.registry)],
            ["--platform-registry", "codex=relative-registry"],
            ["--registry-dir", str(self.registry), "--platform-registry", "codex=" + str(self.registry)],
        ]
        for flags in selections:
            with self.subTest(flags=flags):
                completed = subprocess.run([sys.executable, ROOT / "scripts/installer.py", "install", "--prefix", self.prefix,
                    "--skills-dir", self.skills, "--source", self.source, "--ghost-alice-root", GHOST_ROOT, *flags], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertEqual(json.loads(completed.stderr)["status"], "error")
                self.assertFalse(self.prefix.exists())
                self.assertFalse(self.registry.exists())
                self.assertFalse(self.subject.lock_path.exists())

    def test_existing_single_registry_selection_cannot_silently_expand(self):
        self.subject.install()
        before = self.subject.manifest_path.read_bytes()
        expanded = installer.Installer(self.prefix, [self.skills], self.source, GHOST_ROOT,
            platform_registries={"codex": self.registry, "claude": self.base / "claude-registry"})
        with self.assertRaisesRegex(installer.InstallError, "registry.*uninstall"):
            expanded.status()
        self.assertEqual(self.subject.manifest_path.read_bytes(), before)
        self.assertFalse((self.base / "claude-registry/ai-secretary.json").exists())
        self.assertEqual(self.subject.status()["status"], "installed")

    def test_bare_schema2_single_registry_manifest_remains_unchanged(self):
        self.subject.install()
        manifest = self.subject._manifest()
        self.assertNotIn("platform_registries", manifest)
        for resource in manifest["resources"]:
            resource.pop("platform", None)
        installer._json_write(self.subject.manifest_path, manifest)
        before = self.subject.manifest_path.read_bytes()
        self.assertEqual(self.subject.status()["status"], "installed")
        self.assertEqual(self.subject.install()["status"], "unchanged")
        self.assertEqual(self.subject.manifest_path.read_bytes(), before)

    def test_typed_recorded_uninstaller_removes_every_original_platform(self):
        selected = self._select_two_platform_registries()
        self.subject.install()
        record = self.subject.framework().registry.read_record("ai-secretary", addons_dir=selected["claude"])
        command = next(entry for entry in record["provided"] if entry["name"] == "ai-secretary-runtime")["metadata"]["uninstaller"]
        self.assertNotIn("--registry-dir", command)
        self.assertEqual(command.count("--platform-registry"), 2)
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "uninstalled")
        for directory in selected.values():
            self.assertFalse((directory / "ai-secretary.json").exists())
        self.assertFalse((self.base / "claude-skills/ai-secretary").exists())
        self.assertFalse((self.prefix / "managed").exists())

    def test_second_platform_interruption_recovers_all_platform_records(self):
        selected = self._select_two_platform_registries()
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt_second_record(source, dest):
            original(source, dest)
            if Path(dest) == selected["claude"] / "ai-secretary.json":
                raise KeyboardInterrupt("interrupted second platform publication")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt_second_record):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        self.assertTrue(self.subject.journal_path.exists())
        self.assertEqual(self.subject.status()["status"], "not-installed")
        for directory in selected.values():
            self.assertFalse((directory / "ai-secretary.json").exists())
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse(self.subject.journal_path.exists())

    def test_second_platform_collision_is_refused_before_bundle_provision(self):
        selected = self._select_two_platform_registries()
        selected["claude"].mkdir()
        sidecar = selected["claude"] / "ai-secretary.json"
        sidecar.write_text("another installation owns this record")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual(sidecar.read_text(), "another installation owns this record")
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse((selected["codex"] / "ai-secretary.json").exists())

    def test_second_platform_update_failure_restores_both_original_records(self):
        selected = self._select_two_platform_registries()
        self.subject.install()
        before = {resource["path"]: installer._inventory(resource["path"]) for resource in self.subject._manifest()["resources"]}
        original_manifest = self.subject.manifest_path.read_bytes()
        (self.source / "src/ai_secretary/control.py").write_text("print('changed multi-platform bundle')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def fail_second_record(source, dest):
            if Path(dest) == selected["claude"] / "ai-secretary.json" and Path(source).parent.name == ".ghost-alice-install-staging":
                raise OSError("injected second-platform update failure")
            return original(source, dest)
        with patch.object(framework.transaction, "_rename_path", side_effect=fail_second_record):
            with self.assertRaises(installer.InstallError):
                self.subject.install()
        self.assertEqual(self.subject.manifest_path.read_bytes(), original_manifest)
        for path, entries in before.items():
            self.assertEqual(installer._inventory(path), entries)

    def test_second_platform_live_lock_blocks_the_entire_bundle(self):
        selected = self._select_two_platform_registries()
        selected["claude"].mkdir()
        lock = selected["claude"] / ".ai-secretary.install.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "owner": "another-prefix"}))
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertTrue(lock.is_file())
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse(self.subject.lock_path.exists())
        self.assertFalse((selected["codex"] / "ai-secretary.json").exists())

    def test_interrupted_second_platform_drift_preserves_every_publication(self):
        selected = self._select_two_platform_registries()
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt_second_record(source, dest):
            original(source, dest)
            if Path(dest) == selected["claude"] / "ai-secretary.json":
                raise KeyboardInterrupt("interrupted second platform publication")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt_second_record):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        modified = selected["claude"] / "ai-secretary.json"
        modified.write_text("user modification after interruption")
        with self.assertRaises(installer.InstallError):
            self.subject.status()
        self.assertEqual(modified.read_text(), "user modification after interruption")
        self.assertTrue((selected["codex"] / "ai-secretary.json").is_file())
        self.assertTrue((self.prefix / "managed/bin/openclaw").is_file())
        self.assertTrue(self.subject.journal_path.is_file())

    def test_unowned_registry_collision_is_refused_before_provision(self):
        self.registry.mkdir()
        sidecar = self.registry / "ai-secretary.json"
        sidecar.write_text("user-owned sidecar")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual(sidecar.read_text(), "user-owned sidecar")
        self.assertFalse((self.prefix / "managed").exists())

    def test_registry_drift_blocks_reinstall_and_survives_uninstall(self):
        self.subject.install()
        sidecar = self.registry / "ai-secretary.json"
        self.registry.mkdir(exist_ok=True)
        sidecar.write_text('{"user-customization":"preserve"}')
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        with self.assertRaisesRegex(installer.InstallError, "registry.*uninstall blocked"):
            self.subject.uninstall()
        self.assertEqual(sidecar.read_text(), '{"user-customization":"preserve"}')
        self.assertTrue(self.subject.manifest_path.is_file())
        self.assertTrue((self.prefix / "managed/bin/ai-secretary").is_file())

    def test_uninstall_only_removes_its_own_registry_sidecar(self):
        self.subject.install()
        self.registry.mkdir(exist_ok=True)
        unrelated = self.registry / "other-addon.json"
        unrelated.write_text("other addon attribution")
        self.assertEqual(self.subject.uninstall()["status"], "uninstalled")
        self.assertFalse((self.registry / "ai-secretary.json").exists())
        self.assertEqual(unrelated.read_text(), "other addon attribution")

    def _make_legacy_manifest(self):
        """Convert a clean installation to the pre-registry ownership format."""
        self.subject.install()
        manifest = self.subject._manifest()
        sidecar = self.registry / "ai-secretary.json"
        legacy_registry = self.prefix / "managed/registry/codex"
        legacy_registry.mkdir(parents=True, exist_ok=True)
        if sidecar.exists():
            (legacy_registry / "ai-secretary.json").write_bytes(sidecar.read_bytes())
            sidecar.unlink()
        import shutil
        old_plugin = self.prefix / "managed/plugin"
        if old_plugin.exists() or old_plugin.is_symlink():
            if old_plugin.is_symlink():
                old_plugin.unlink()
            else:
                shutil.rmtree(old_plugin)
        shutil.copytree(self.prefix / "managed/app/openclaw-plugin", old_plugin)
        manifest["schema_version"] = 1
        manifest.pop("registry_dir", None)
        manifest["resources"] = [entry for entry in manifest["resources"] if entry["role"] != "registry"]
        manifest["resources"][0]["entries"] = installer._inventory(self.prefix / "managed")
        installer._json_write(self.subject.manifest_path, manifest)
        state_config = self.prefix / "state/openclaw.json"
        config = json.loads(state_config.read_text())
        config["plugins"]["load"]["paths"] = [str(old_plugin)]
        config["user-customization"] = "preserve"
        state_config.write_text(json.dumps(config))
        return state_config.read_bytes()

    def test_clean_legacy_manifest_migrates_without_editing_mutable_state(self):
        configuration = self._make_legacy_manifest()
        state = self.prefix / "state/secretary/user-work.txt"
        state.write_text("persistent work")
        self.assertEqual(self.subject.install()["status"], "installed")
        self.assertEqual(self.subject._manifest()["schema_version"], 2)
        records = self.subject.framework().registry.read_all(addons_dir=self.registry)
        self.assertEqual([record["addon_id"] for record in records], ["ai-secretary"])
        self.assertEqual((self.prefix / "state/openclaw.json").read_bytes(), configuration)
        self.assertEqual(state.read_text(), "persistent work")
        self.assertTrue((self.prefix / "managed/plugin").is_symlink())
        self.assertEqual((self.prefix / "managed/plugin").resolve(), self.prefix / "managed/app/openclaw-plugin")

    def test_legacy_manifest_does_not_claim_an_existing_registry_record(self):
        self._make_legacy_manifest()
        self.registry.mkdir(exist_ok=True)
        sidecar = self.registry / "ai-secretary.json"
        sidecar.write_text("another prefix owns this")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual(sidecar.read_text(), "another prefix owns this")
        self.subject.uninstall()
        self.assertEqual(sidecar.read_text(), "another prefix owns this")

    def test_installed_plugin_resolves_packaged_resources(self):
        plugin = self.source / "openclaw-plugin/index.mjs"
        plugin.write_text("import schema from '../src/ai_secretary/resources/schemas/desktop-tool-schemas.mjs';\n"
            "import {canonicalGate} from '../src/ai_secretary/resources/ga-gate.mjs';\n"
            "if (!schema.canonical || !canonicalGate) throw new Error('canonical resources missing');\nexport default {};\n")
        self.subject.install()
        config = json.loads((self.prefix / "state/openclaw.json").read_text())
        plugin_root = Path(config["plugins"]["load"]["paths"][0])
        import shutil
        completed = subprocess.run([shutil.which("node"), plugin_root / "index.mjs"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(plugin_root, self.prefix / "managed/app/openclaw-plugin")
        self.assertFalse((self.prefix / "managed/plugin").exists())
        self.assertTrue((self.prefix / "managed/app/pyproject.toml").is_file())

    def test_registry_publication_recovers_after_interruption(self):
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt(source, dest):
            original(source, dest)
            if Path(dest) == self.registry / "ai-secretary.json":
                raise KeyboardInterrupt("interrupted registry publication")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        self.assertTrue(self.subject.journal_path.exists())
        self.assertEqual(self.subject.status()["status"], "not-installed")
        self.assertFalse((self.registry / "ai-secretary.json").exists())
        self.assertFalse((self.skills / "ai-secretary").exists())
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse(self.subject.journal_path.exists())

    def test_registry_selection_cannot_change_for_an_owned_install(self):
        self.subject.install()
        original = self.subject.registry_dir
        self.subject.registry_dir = self.base / "other-registry"
        self.subject.registry_path = self.subject.registry_dir / "ai-secretary.json"
        try:
            with self.assertRaises(installer.InstallError):
                self.subject.status()
        finally:
            self.subject.registry_dir = original
            self.subject.registry_path = original / "ai-secretary.json"

    def test_cli_registry_defaults_to_normal_ghost_alice_discovery(self):
        from unittest.mock import MagicMock
        mocked = MagicMock()
        mocked.status.return_value = {"status": "not-installed"}
        with patch.object(installer, "Installer", return_value=mocked) as constructor:
            with patch("builtins.print"):
                self.assertEqual(installer.main(["status"]), 0)
        self.assertEqual(constructor.call_args.kwargs.get("registry_dir"), Path.home() / ".ghost-alice/addons")

    def test_registry_attribution_describes_runtime_and_owned_launchers(self):
        self.subject.install()
        record = self.subject.framework().registry.read_record("ai-secretary", addons_dir=self.registry)
        runtime = [entry for entry in record["provided"] if entry["kind"] == "resource" and entry["name"] == "ai-secretary-runtime"]
        self.assertEqual(len(runtime), 1)
        self.assertEqual(runtime[0]["target"], str(self.prefix / "managed"))
        self.assertEqual(runtime[0]["content_hash"], self.subject.framework().addon.hash_utils.hash_target(self.prefix / "managed", "copy"))
        self.assertEqual(runtime[0]["metadata"]["uninstaller"], [str(Path(sys.executable).resolve()), str(self.prefix / "managed/app/scripts/installer.py"),
            "uninstall", "--prefix", str(self.prefix), "--registry-dir", str(self.registry), "--skills-dir", str(self.skills),
            "--ghost-alice-root", str(self.subject.ghost_root)])
        for name in ("ai-secretary", "ai-secretary-worker", "openclaw"):
            self.assertEqual(runtime[0]["metadata"]["entrypoints"][name], str(self.prefix / "managed/bin" / name))

    def test_other_prefix_cannot_take_over_the_registered_addon(self):
        self.subject.install()
        before = (self.registry / "ai-secretary.json").read_bytes()
        other = installer.Installer(self.base / "other-prefix", [self.base / "other-skills"], self.source,
            self.subject.ghost_root, registry_dir=self.registry)
        with self.assertRaises(installer.InstallError):
            other.install()
        self.assertEqual((self.registry / "ai-secretary.json").read_bytes(), before)
        self.assertFalse((other.prefix / "managed").exists())

    def test_registry_lock_serializes_the_same_addon_across_prefixes(self):
        self.registry.mkdir()
        lock = self.registry / ".ai-secretary.install.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "owner": "another-prefix"}))
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertTrue(lock.exists())
        self.assertFalse(self.subject.lock_path.exists())

    def test_legacy_plugin_alias_loads_canonical_resources_and_survives_updates(self):
        plugin = self.source / "openclaw-plugin/index.mjs"
        plugin.write_text("import {canonicalGate} from '../src/ai_secretary/resources/ga-gate.mjs';\n"
            "if (!canonicalGate) throw new Error('canonical gate missing');\nexport default {};\n")
        self._make_legacy_manifest()
        self.subject.install()
        import shutil
        command = [shutil.which("node"), self.prefix / "managed/plugin/index.mjs"]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (self.source / "src/ai_secretary/control.py").write_text("print('updated')\n")
        self.subject.install()
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.prefix / "managed/plugin").is_symlink())

    def test_interrupted_sidecar_drift_blocks_recovery_without_deleting_resources(self):
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt(source, dest):
            original(source, dest)
            if Path(dest) == self.registry / "ai-secretary.json":
                raise KeyboardInterrupt("interrupted registry publication")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        sidecar = self.registry / "ai-secretary.json"
        sidecar.write_text("user change after interruption")
        with self.assertRaises(installer.InstallError):
            self.subject.status()
        self.assertEqual(sidecar.read_text(), "user change after interruption")
        self.assertTrue((self.prefix / "managed").exists())
        self.assertTrue(self.subject.journal_path.exists())

    def test_registry_symlink_drift_is_preserved_without_following_it(self):
        self.subject.install()
        sidecar = self.registry / "ai-secretary.json"
        sidecar.unlink()
        valuable = self.base / "valuable-attribution.json"
        valuable.write_text("valuable unrelated registry")
        sidecar.symlink_to(valuable)
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        with self.assertRaisesRegex(installer.InstallError, "registry.*uninstall blocked"):
            self.subject.uninstall()
        self.assertTrue(sidecar.is_symlink())
        self.assertEqual(valuable.read_text(), "valuable unrelated registry")
        self.assertTrue(self.subject.manifest_path.is_file())
        self.assertTrue((self.skills / "ai-secretary/SKILL.md").is_file())

    def test_installed_uninstaller_uses_persisted_registry_arguments(self):
        self.subject.install()
        sidecar = self.subject.framework().registry.read_record("ai-secretary", addons_dir=self.registry)
        runtime = next(entry for entry in sidecar["provided"] if entry["name"] == "ai-secretary-runtime")
        completed = subprocess.run(runtime["metadata"]["uninstaller"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "uninstalled")
        self.assertFalse((self.registry / "ai-secretary.json").exists())
        self.assertFalse((self.prefix / "managed").exists())
        self.assertTrue((self.prefix / "state/openclaw.json").exists())

    def test_registry_parent_symlink_is_rejected_before_writing(self):
        target = self.base / "user-registry"
        target.mkdir()
        alias = self.base / "registry-alias"
        alias.symlink_to(target, target_is_directory=True)
        other = installer.Installer(self.base / "other-prefix", [self.base / "other-skills"], self.source,
            self.subject.ghost_root, registry_dir=alias / "nested")
        with self.assertRaises(installer.InstallError):
            other.install()
        self.assertEqual(list(target.iterdir()), [])

    def test_legacy_drift_blocks_migration_and_preserves_user_files(self):
        self._make_legacy_manifest()
        added = self.prefix / "managed/user-note.txt"
        added.write_text("preserved legacy note")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual(added.read_text(), "preserved legacy note")
        self.assertFalse((self.registry / "ai-secretary.json").exists())

    def test_registry_publish_failure_restores_previous_sidecar(self):
        self.subject.install()
        sidecar = self.registry / "ai-secretary.json"
        before = sidecar.read_bytes()
        manifest_before = self.subject.manifest_path.read_bytes()
        (self.source / "src/ai_secretary/control.py").write_text("print('new update')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def fail_manifest(source, dest):
            if Path(dest) == self.subject.manifest_path and Path(source).parent.name == ".ghost-alice-install-staging":
                raise OSError("failed after registry publication")
            original(source, dest)
        with patch.object(framework.transaction, "_rename_path", side_effect=fail_manifest):
            with self.assertRaises(installer.InstallError):
                self.subject.install()
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(self.subject.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(self.subject.status()["status"], "installed")

    def test_valid_customized_registry_blocks_uninstall_before_any_deletion(self):
        self.subject.install()
        registry = self.subject.framework().registry
        record = registry.read_record("ai-secretary", addons_dir=self.registry)
        record["installation"]["user_note"] = "preserve my local note"
        registry.write_record(record, addons_dir=self.registry)
        before = {resource["path"]: installer._inventory(resource["path"]) for resource in self.subject._manifest()["resources"]}
        manifest_before = self.subject.manifest_path.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "registry.*uninstall blocked"):
            self.subject.uninstall()
        self.assertEqual(registry.read_all_ids(addons_dir=self.registry), ["ai-secretary"])
        self.assertEqual(registry.read_record("ai-secretary", addons_dir=self.registry)["installation"]["user_note"], "preserve my local note")
        self.assertEqual(self.subject.manifest_path.read_bytes(), manifest_before)
        for path, entries in before.items():
            self.assertEqual(installer._inventory(path), entries)
        for name in ("ai-secretary", "ai-secretary-worker", "openclaw"):
            self.assertTrue((self.prefix / "managed/bin" / name).is_file())

    def test_missing_registry_sidecar_does_not_block_uninstall(self):
        self.subject.install()
        (self.registry / "ai-secretary.json").unlink()
        try:
            result = self.subject.uninstall()
        except installer.InstallError as exc:
            self.fail(f"absent attribution cannot dangle and should not block removal: {exc}")
        self.assertEqual(result["status"], "uninstalled")
        self.assertFalse(self.subject.manifest_path.exists())
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse((self.skills / "ai-secretary").exists())
        self.assertEqual(self.subject.framework().registry.read_all_ids(addons_dir=self.registry), [])

    def test_partial_uninstall_preserves_the_recorded_command_for_subprocess_retry(self):
        self.subject.install()
        record = self.subject.framework().registry.read_record("ai-secretary", addons_dir=self.registry)
        command = next(entry for entry in record["provided"] if entry["name"] == "ai-secretary-runtime")["metadata"]["uninstaller"]
        state = self.prefix / "state/secretary/user-work.txt"
        state.write_text("preserve persistent work")
        original = Path.unlink
        def fail_owned_file(path, *args, **kwargs):
            if path == self.prefix / "managed/bin/openclaw":
                raise PermissionError("injected owned-file removal failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", new=fail_owned_file):
            with self.assertRaises(PermissionError):
                self.subject.uninstall()
        self.assertFalse((self.registry / "ai-secretary.json").exists())
        self.assertTrue(self.subject.manifest_path.is_file())
        self.assertTrue((self.prefix / "managed/bin/openclaw").is_file())
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "uninstalled")
        self.assertFalse(self.subject.manifest_path.exists())
        self.assertFalse((self.prefix / "managed").exists())
        self.assertFalse((self.skills / "ai-secretary").exists())
        self.assertEqual(state.read_text(), "preserve persistent work")
        self.assertEqual(self.subject.framework().registry.read_all_ids(addons_dir=self.registry), [])

    def test_user_modified_packaged_uninstaller_is_preserved(self):
        self.subject.install()
        script = self.prefix / "managed/app/scripts/installer.py"
        script.write_text("user customized uninstall program\n")
        result = self.subject.uninstall()
        self.assertEqual(result["status"], "preserved")
        self.assertIn(str(script), result["preserved"])
        self.assertEqual(script.read_text(), "user customized uninstall program\n")
        self.assertFalse((self.registry / "ai-secretary.json").exists())

    def _assert_final_unlink_failure_allows_packaged_retry(self, failure_path):
        self.subject.install()
        record = self.subject.framework().registry.read_record("ai-secretary", addons_dir=self.registry)
        command = next(entry for entry in record["provided"] if entry["name"] == "ai-secretary-runtime")["metadata"]["uninstaller"]
        original = Path.unlink
        def fail_final_file(path, *args, **kwargs):
            if path == failure_path:
                raise PermissionError("injected final removal failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", new=fail_final_file):
            with self.assertRaises(PermissionError):
                self.subject.uninstall()
        self.assertTrue(self.subject.manifest_path.is_file())
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "uninstalled")
        self.assertFalse(self.subject.manifest_path.exists())
        self.assertFalse((self.prefix / "managed").exists())

    def test_manifest_unlink_failure_preserves_the_recorded_retry_command(self):
        self._assert_final_unlink_failure_allows_packaged_retry(self.subject.manifest_path)

    def test_packaged_script_unlink_failure_restores_manifest_for_retry(self):
        self._assert_final_unlink_failure_allows_packaged_retry(self.prefix / "managed/app/scripts/installer.py")

    def test_install_reinstall_uninstall_preserves_state_and_unrelated_files(self):
        self.prefix.mkdir()
        sentinel = self.prefix / "personal-note.txt"
        sentinel.write_text("keep me")
        first = self.subject.install()
        self.assertEqual(first["status"], "installed")
        self.assertTrue((self.skills / "ai-secretary/SKILL.md").is_file())
        self.assertEqual(self.subject.status()["status"], "installed")
        state = self.prefix / "state/secretary/user-task.sqlite3"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("durable user task")
        self.assertEqual(self.subject.install()["status"], "unchanged")
        result = self.subject.uninstall()
        self.assertEqual(result["status"], "uninstalled")
        self.assertFalse((self.skills / "ai-secretary").exists())
        self.assertFalse((self.prefix / "managed/bin/ai-secretary").exists())
        self.assertEqual(state.read_text(), "durable user task")
        self.assertEqual(sentinel.read_text(), "keep me")

    def test_existing_unowned_skill_is_never_overwritten(self):
        skill = self.skills / "ai-secretary"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("user-owned")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual((skill / "SKILL.md").read_text(), "user-owned")
        self.assertFalse((self.prefix / "managed").exists())

    def test_failed_reinstall_retains_the_entire_previous_install(self):
        self.subject.install()
        old = (self.prefix / "install-manifest.json").read_bytes()
        (self.source / "src/ai_secretary/control.py").write_text("print('updated')\n")
        (self.source / "scripts/build_native.sh").write_text("#!/bin/sh\nexit 7\n")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual((self.prefix / "install-manifest.json").read_bytes(), old)
        self.assertEqual((self.prefix / "managed/app/src/ai_secretary/control.py").read_text(), "print('fixture')\n")
        self.assertTrue((self.skills / "ai-secretary/SKILL.md").is_file())

    def test_drift_blocks_reinstall_and_uninstall_preserves_modified_file(self):
        self.subject.install()
        modified = self.skills / "ai-secretary/SKILL.md"
        modified.write_text("user customization")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        result = self.subject.uninstall()
        self.assertEqual(result["status"], "preserved")
        self.assertIn(str(modified), result["preserved"])
        self.assertEqual(modified.read_text(), "user customization")

    def test_added_file_under_managed_resource_is_preserved(self):
        self.subject.install()
        added = self.prefix / "managed/app/user-notes.txt"
        added.write_text("keep")
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.subject.uninstall()
        self.assertEqual(added.read_text(), "keep")

    def test_symlink_target_drift_is_preserved_without_deleting_its_target(self):
        self.subject.install()
        launcher = self.prefix / "managed/bin/ai-secretary"
        launcher.unlink()
        target = self.base / "valuable"
        target.write_text("user data")
        launcher.symlink_to(target)
        result = self.subject.uninstall()
        self.assertIn(str(launcher), result["preserved"])
        self.assertEqual(target.read_text(), "user data")

    def test_failure_during_publish_rolls_back_all_targets(self):
        self.subject.install()
        old = (self.prefix / "install-manifest.json").read_bytes()
        (self.source / "src/ai_secretary/control.py").write_text("print('new')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def fail_skill_publish(source, dest):
            if Path(dest) == self.skills / "ai-secretary" and "stage" in Path(source).name:
                raise OSError("injected publish failure")
            return original(source, dest)
        with patch.object(framework.transaction, "_rename_path", side_effect=fail_skill_publish):
            with self.assertRaises(installer.InstallError):
                self.subject.install()
        self.assertEqual((self.prefix / "install-manifest.json").read_bytes(), old)
        self.assertEqual((self.prefix / "managed/app/src/ai_secretary/control.py").read_text(), "print('fixture')\n")

    def test_cached_runtime_pin_mismatch_is_rejected_before_publish(self):
        (self.runtime / "node_modules/openclaw/package.json").write_text('{"version":"2026.9.9"}')
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertFalse((self.prefix / "managed").exists())

    def test_live_lock_is_never_stolen_when_old(self):
        lock = self.subject.lock_path
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid()}))
        os.utime(lock, (1, 1))
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertTrue(lock.is_file())

    def test_interrupted_publication_recovers_previous_install_on_next_call(self):
        self.subject.install()
        old = (self.prefix / "install-manifest.json").read_bytes()
        (self.source / "src/ai_secretary/control.py").write_text("print('interrupted update')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt_after_managed_publish(source, dest):
            original(source, dest)
            if Path(dest) == self.prefix / "managed" and "stage" in Path(source).name:
                raise KeyboardInterrupt("simulated interrupted installer")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt_after_managed_publish):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        self.assertTrue(self.subject.journal_path.exists())
        self.assertEqual(self.subject.status()["status"], "installed")
        self.assertEqual((self.prefix / "install-manifest.json").read_bytes(), old)
        self.assertEqual((self.prefix / "managed/app/src/ai_secretary/control.py").read_text(), "print('fixture')\n")
        self.assertFalse(self.subject.journal_path.exists())

    def test_replaced_parent_symlink_never_causes_uninstall_to_delete_outside_tree(self):
        self.subject.install()
        target = self.base / "user-app"
        target.mkdir()
        (target / "__init__.py").write_text("user file")
        app = self.prefix / "managed/app/src/ai_secretary"
        import shutil
        shutil.rmtree(app)
        app.symlink_to(target, target_is_directory=True)
        result = self.subject.uninstall()
        self.assertEqual(result["status"], "preserved")
        self.assertEqual((target / "__init__.py").read_text(), "user file")

    def test_untouched_reinstall_does_not_hide_framework_or_runtime_changes(self):
        self.subject.install()
        manifest = json.loads((self.prefix / "install-manifest.json").read_text())
        manifest["node_version"] = "24.16.0"
        (self.prefix / "install-manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.subject.install()["status"], "installed")
        self.assertEqual(self.subject.status()["node_version"], "24.21.0")

    def test_cache_integrity_metadata_alone_is_insufficient(self):
        subject = installer.Installer(self.prefix, [self.skills], self.source,
            GHOST_ROOT, registry_dir=self.registry, openclaw_source=self.runtime, node_source=self.node)
        with self.assertRaises(installer.InstallError):
            subject.install()
        self.assertFalse((self.prefix / "managed").exists())

    def test_runtime_config_and_gateway_token_are_persistent_user_state(self):
        self.subject.install()
        token = self.prefix / "state/gateway.token"
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        initial_token = token.read_text()
        configuration = self.prefix / "state/openclaw.json"
        data = json.loads(configuration.read_text())
        self.assertEqual(data["gateway"]["auth"]["mode"], "token")
        data["user-customization"] = "preserve"
        configuration.write_text(json.dumps(data))
        self.assertEqual(self.subject.status()["status"], "installed")
        self.assertEqual(self.subject.install()["status"], "unchanged")
        self.subject.uninstall()
        self.assertEqual(token.read_text(), initial_token)
        self.assertEqual(json.loads(configuration.read_text())["user-customization"], "preserve")

    def test_private_runtime_profile_admits_user_intake_and_disables_cloud_fallbacks(self):
        self.subject.install()
        config = json.loads((self.prefix / "state/openclaw.json").read_text())
        entry = config["plugins"]["entries"]["ai-secretary"]
        self.assertEqual(entry.get("hooks"), {"allowConversationAccess": True, "allowPromptInjection": True})
        self.assertEqual(config["plugins"].get("slots"), {"memory": "none"})
        self.assertEqual(config.get("channels"), {})
        self.assertEqual(config.get("logging", {}).get("file"), str(self.prefix / "state/openclaw/gateway.log"))
        self.assertEqual(config["tools"].get("media"), {kind: {"enabled": False} for kind in ("image", "audio", "video")})

    def test_state_symlink_refuses_install_without_touching_its_target(self):
        self.prefix.mkdir()
        user_state = self.base / "valuable-state"
        user_state.mkdir()
        (self.prefix / "state").symlink_to(user_state, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            self.subject.install()
        self.assertEqual(list(user_state.iterdir()), [])
        self.assertFalse((self.prefix / "managed").exists())

    def test_plugin_cli_and_worker_share_the_same_governance_ledger_root(self):
        for module in ("control", "service"):
            (self.source / f"src/ai_secretary/{module}.py").write_text("import os\nprint(os.environ['SECRETARY_GA_LEDGER_ROOT'])\n")
        self.subject.install()
        expected = str(self.prefix / "state/ghost-alice")
        for command in ("ai-secretary", "ai-secretary-worker"):
            completed = subprocess.run([self.prefix / "managed/bin" / command], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), expected)
        config = json.loads((self.prefix / "state/openclaw.json").read_text())
        self.assertEqual(config["plugins"]["entries"]["ai-secretary"]["config"]["gaLedgerRoot"], expected)

    def test_control_launcher_uses_the_managed_openclaw_binary(self):
        (self.source / "src/ai_secretary/control.py").write_text("import os\nprint(os.environ.get('SECRETARY_OPENCLAW_BINARY', 'missing'))\n")
        self.subject.install()
        completed = subprocess.run([self.prefix / "managed/bin/ai-secretary"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(self.prefix / "managed/bin/openclaw"))

    def test_package_initialization_finishes_before_the_ownership_snapshot(self):
        pending = self.runtime / "node_modules/openclaw/.openclaw-lifecycle-pending"
        pending.write_text("fixture initialization marker")
        binary = self.node / "bin/node"
        binary.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\necho v24.21.0\nelse\npackage_dir=$(dirname \"$1\")\nrm -f \"$package_dir/.openclaw-lifecycle-pending\"\necho 'OpenClaw 2026.9.8'\nfi\n")
        self.subject.install()
        self.assertFalse((self.prefix / "managed/openclaw/node_modules/openclaw/.openclaw-lifecycle-pending").exists())
        self.assertEqual(self.subject.status()["status"], "installed")

    def test_recovery_preserves_user_additions_to_an_interrupted_publication(self):
        self.subject.install()
        (self.source / "src/ai_secretary/control.py").write_text("print('interrupted update')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt(source, dest):
            original(source, dest)
            if Path(dest) == self.prefix / "managed" and "stage" in Path(source).name:
                raise KeyboardInterrupt("interrupted publication")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        added = self.prefix / "managed/user-note.txt"
        added.write_text("valuable user note")
        with self.assertRaises(installer.InstallError):
            self.subject.status()
        self.assertEqual(added.read_text(), "valuable user note")
        self.assertTrue(self.subject.journal_path.exists())

    def test_recovery_can_itself_be_interrupted_and_then_resumed(self):
        self.subject.install()
        (self.source / "src/ai_secretary/control.py").write_text("print('interrupted update')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        def interrupt_publication(source, dest):
            original(source, dest)
            if Path(dest) == self.prefix / "managed" and "stage" in Path(source).name:
                raise KeyboardInterrupt("interrupted publication")
        with patch.object(framework.transaction, "_rename_path", side_effect=interrupt_publication):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        replace = os.replace
        def interrupt_recovery(source, dest):
            replace(source, dest)
            if Path(dest) == self.prefix / "managed" and Path(source).parent.name.endswith(".rollbacks"):
                raise KeyboardInterrupt("interrupted recovery")
        with patch.object(installer.os, "replace", side_effect=interrupt_recovery):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.status()
        self.assertEqual(self.subject.status()["status"], "installed")
        self.assertEqual((self.prefix / "managed/app/src/ai_secretary/control.py").read_text(), "print('fixture')\n")

    def test_hidden_user_additions_block_reinstall_and_survive_uninstall(self):
        self.subject.install()
        for name in (".git", "__pycache__", ".pytest_cache"):
            with self.subTest(directory=name):
                note = self.prefix / "managed/app" / name / "user-note.txt"
                note.parent.mkdir()
                note.write_text("user-owned hidden note")
                (self.source / "src/ai_secretary/control.py").write_text("print('update with hidden note')\n")
                with self.assertRaises(installer.InstallError):
                    self.subject.install()
                self.assertEqual(note.read_text(), "user-owned hidden note")
        result = self.subject.uninstall()
        self.assertEqual(result["status"], "preserved")
        for name in (".git", "__pycache__", ".pytest_cache"):
            self.assertEqual((self.prefix / "managed/app" / name / "user-note.txt").read_text(), "user-owned hidden note")

    def test_journal_traversal_cannot_move_or_delete_persistent_user_state(self):
        self.subject.install()
        valuable = self.prefix / "state/secretary/user-work.txt"
        valuable.write_text("valuable persistent work")
        work = self.prefix.parent / ("." + self.prefix.name + ".stage-spoof")
        work.mkdir()
        self.subject.journal_path.write_text(json.dumps({"schema_version": 1, "prefix": str(self.prefix),
            "work_root": str(work), "moves": [{"source": str(work / "restored"),
                "dest": str(self.prefix / "managed/../state/secretary/user-work.txt")}]}))
        with self.assertRaises(installer.InstallError):
            self.subject.status()
        self.assertEqual(valuable.read_text(), "valuable persistent work")
        self.assertTrue(self.subject.journal_path.exists())

    def test_relative_dot_skills_dir_selects_the_current_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.base)
            subject = installer.Installer(self.prefix, ["."], self.source, GHOST_ROOT, registry_dir=self.registry)
        finally:
            os.chdir(previous)
        self.assertEqual(subject.skills_dirs, [self.base])

    def test_invalid_addon_cli_returns_json_error(self):
        (self.source / "addons/ai-secretary/addon.json").write_text('{"addon_id":"invalid/id"}')
        completed = subprocess.run([sys.executable, ROOT / "scripts/installer.py", "install",
            "--prefix", self.prefix, "--skills-dir", self.skills, "--registry-dir", self.registry, "--source", self.source,
            "--ghost-alice-root", str(GHOST_ROOT)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stderr)["status"], "error")

    def test_ordinary_publish_failure_preserves_new_user_file_before_rollback(self):
        self.subject.install()
        (self.source / "src/ai_secretary/control.py").write_text("print('new update')\n")
        framework = self.subject.framework()
        original = framework.transaction._rename_path
        note = self.prefix / "managed/user-note.txt"
        def fail_after_addition(source, dest):
            if Path(dest) == self.skills / "ai-secretary" and "stage" in Path(source).name:
                note.write_text("valuable concurrent note")
                raise OSError("publication failed after a user addition")
            return original(source, dest)
        with patch.object(framework.transaction, "_rename_path", side_effect=fail_after_addition):
            with self.assertRaises(installer.InstallError):
                self.subject.install()
        self.assertEqual(note.read_text(), "valuable concurrent note")
        self.assertTrue(self.subject.journal_path.exists())

    def test_committed_interrupted_cleanup_preserves_changed_old_backup(self):
        self.subject.install()
        (self.source / "src/ai_secretary/control.py").write_text("print('new committed update')\n")
        write = installer._json_write
        def interrupt(path, value):
            write(path, value)
            if Path(path) == self.subject.journal_path and value.get("committed"):
                raise KeyboardInterrupt("interrupted committed cleanup")
        with patch.object(installer, "_json_write", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        journal = json.loads(self.subject.journal_path.read_text())
        rollback = next(Path(move["dest"]) for move in journal["moves"] if move["source"] == str(self.prefix / "managed"))
        note = rollback / "user-note.txt"
        note.write_text("valuable old-backup note")
        with self.assertRaises(installer.InstallError):
            self.subject.status()
        self.assertEqual(note.read_text(), "valuable old-backup note")
        self.assertTrue(self.subject.journal_path.exists())

    def test_invalid_journal_manifest_and_lock_shapes_fail_closed(self):
        self.prefix.mkdir()
        for name, path in [("journal", self.subject.journal_path), ("manifest", self.subject.manifest_path), ("lock", self.subject.lock_path)]:
            with self.subTest(document=name):
                path.write_text("[]")
                try:
                    with self.assertRaises(installer.InstallError):
                        self.subject.status()
                finally:
                    path.unlink()

    def test_cleanup_permission_failure_retains_journal_until_successful_retry(self):
        self.subject.install()
        (self.source / "src/ai_secretary/control.py").write_text("print('new committed update')\n")
        write = installer._json_write
        def interrupt(path, value):
            write(path, value)
            if Path(path) == self.subject.journal_path and value.get("committed"):
                raise KeyboardInterrupt("interrupted committed cleanup")
        with patch.object(installer, "_json_write", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.install()
        journal = json.loads(self.subject.journal_path.read_text())
        rollback = next(Path(move["dest"]) for move in journal["moves"] if move["source"] == str(self.prefix / "managed"))
        readonly = rollback / "app/src/ai_secretary"
        readonly.chmod(0o500)
        try:
            with self.assertRaises(installer.InstallError):
                self.subject.status()
            self.assertTrue(self.subject.journal_path.exists())
        finally:
            if readonly.exists():
                readonly.chmod(0o755)
        self.assertEqual(self.subject.status()["status"], "installed")
        self.assertFalse(self.subject.journal_path.exists())
        self.assertFalse(rollback.exists())


@unittest.skipUnless(os.environ.get("SECRETARY_TEST_OPENCLAW_SOURCE"), "set SECRETARY_TEST_OPENCLAW_SOURCE for pinned runtime smoke")
class PinnedRuntimeTests(unittest.TestCase):
    def _assert_native_plugin_loaded(self, prefix, label):
        completed = subprocess.run([prefix / "managed/bin/openclaw", "plugins", "inspect", "ai-secretary", "--runtime", "--json"],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        report = json.loads(completed.stdout)
        evidence = ROOT / ".tmp/verification/independent-addon-installer"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / (label + "-plugin-report.json")).write_text(json.dumps(report, indent=2) + "\n")
        self.assertEqual(report["plugin"]["status"], "loaded", json.dumps(report))
        print(label + "_native_plugin_status=" + report["plugin"]["status"], flush=True)

    def test_real_pinned_install_config_reinstall_and_preserving_uninstall(self):
        with tempfile.TemporaryDirectory(prefix="secretary-real-install-") as temporary:
            base = Path(temporary).resolve()
            prefix, skills = base / "prefix", base / "skills"
            subject = installer.Installer(prefix, [skills], ROOT, GHOST_ROOT,
                registry_dir=base / "registry", openclaw_source=os.environ["SECRETARY_TEST_OPENCLAW_SOURCE"])
            prefix.mkdir()
            sentinel = prefix / "unrelated.txt"
            sentinel.write_text("owned by user")
            result = subject.install()
            self.assertEqual(result["status"], "installed")
            records = subject.framework().registry.read_all(addons_dir=base / "registry")
            self.assertEqual([record["addon_id"] for record in records], ["ai-secretary"])
            manifest = subject._manifest()
            print("installed_source_fingerprint=" + manifest["source_fingerprint"], flush=True)
            print("installed_installer_fingerprint=" + manifest["installer_fingerprint"], flush=True)
            version = subprocess.run([prefix / "managed/bin/openclaw", "--version"],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertIn("2026.9.8", version.stdout)
            check = subprocess.run([prefix / "managed/bin/openclaw", "config", "validate"],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(check.returncode, 0, check.stderr + check.stdout)
            skills_list = subprocess.run([prefix / "managed/bin/openclaw", "skills", "list", "--json"],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(skills_list.returncode, 0, skills_list.stderr + skills_list.stdout)
            catalog = json.loads(skills_list.stdout)
            self.assertIn("ai-secretary", [skill["name"] for skill in catalog["skills"]])
            self._assert_native_plugin_loaded(prefix, "canonical")
            self.assertTrue((prefix / "managed/app/src/ai_secretary/resources/tool-schemas.json").is_file())
            self.assertTrue((prefix / "managed/app/src/ai_secretary/resources/model-policy.json").is_file())
            self.assertTrue((prefix / "managed/app/src/ai_secretary/resources/ga-gate.mjs").is_file())
            self.assertFalse((prefix / "managed/app/build/TestWindow.app").exists())
            self.assertEqual(subject.status()["status"], "installed")
            self.assertEqual(subject.install()["status"], "unchanged")
            state = prefix / "state/secretary/user-work.txt"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("preserve work")
            # Exercise a clean schema-1 install's old config path using the
            # real plugin loader, while preserving the mutable config bytes.
            import shutil
            legacy_plugin = prefix / "managed/plugin"
            shutil.copytree(prefix / "managed/app/openclaw-plugin", legacy_plugin)
            legacy_registry = prefix / "managed/registry/codex"
            legacy_registry.mkdir(parents=True)
            sidecar = base / "registry/ai-secretary.json"
            shutil.copy2(sidecar, legacy_registry / "ai-secretary.json")
            sidecar.unlink()
            legacy_manifest = subject._manifest()
            legacy_manifest["schema_version"] = 1
            legacy_manifest.pop("registry_dir")
            legacy_manifest["resources"] = [resource for resource in legacy_manifest["resources"] if resource["role"] != "registry"]
            legacy_manifest["resources"][0]["entries"] = installer._inventory(prefix / "managed")
            installer._json_write(subject.manifest_path, legacy_manifest)
            config_path = prefix / "state/openclaw.json"
            config = json.loads(config_path.read_text())
            config["plugins"]["load"]["paths"] = [str(legacy_plugin)]
            config["gateway"]["port"] = 17654
            config_path.write_text(json.dumps(config))
            original_config = config_path.read_bytes()
            self.assertEqual(subject.install()["status"], "installed")
            self.assertEqual(config_path.read_bytes(), original_config)
            self.assertTrue(legacy_plugin.is_symlink())
            self._assert_native_plugin_loaded(prefix, "migrated")
            self.assertEqual(subject.framework().registry.read_all_ids(addons_dir=base / "registry"), ["ai-secretary"])
            self.assertEqual(state.read_text(), "preserve work")
            self.assertEqual(subject.install()["status"], "unchanged")
            added = prefix / "managed/user-note.txt"
            added.write_text("preserve note")
            with self.assertRaises(installer.InstallError):
                subject.install()
            result = subject.uninstall()
            self.assertEqual(result["status"], "preserved")
            self.assertEqual(added.read_text(), "preserve note")
            self.assertEqual(state.read_text(), "preserve work")
            self.assertEqual(sentinel.read_text(), "owned by user")
            self.assertFalse((skills / "ai-secretary").exists())
            self.assertFalse((prefix / "managed/bin/openclaw").exists())
            self.assertFalse((base / "registry/ai-secretary.json").exists())


if __name__ == "__main__":
    unittest.main()
