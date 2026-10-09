#!/usr/bin/env python3
"""Validate the committed source absorption before optional native builds."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
pin = json.loads((root / "UPSTREAM.json").read_text())
manifest = root / "upstream/selected-source-hashes.json"
assert hashlib.sha256(manifest.read_bytes()).hexdigest() == pin["source_manifest_sha256"], "source manifest changed"
patch = root / "upstream/secretary-owned-lanes.patch"
assert hashlib.sha256(patch.read_bytes()).hexdigest() == pin["patch_sha256"], "source patch changed"
sources = json.loads(manifest.read_text())
vendor = root / "Vendor/Peekaboo"
actual_files = set()
for path in vendor.rglob("*"):
    assert not path.is_symlink(), f"vendor symlink is forbidden: {path.relative_to(vendor)}"
    if path.is_file():
        actual_files.add(path.relative_to(vendor).as_posix())
assert actual_files == {item["path"] for item in sources}, "unexpected or missing vendor source"
for item in sources:
    path = root / "Vendor/Peekaboo" / item["path"]
    assert path.is_file() and not path.is_symlink(), f"pinned source absent or linked: {item['path']}"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == item["absorbed_sha256"], f"pinned source changed: {item['path']}"
for package in pin["licenses"]:
    path = root / package["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == package["sha256"], f"notice changed: {package['path']}"
expected = {item["identity"]: item["state"] for item in json.loads((root / "upstream/Package.resolved").read_text())["pins"]
            if item["identity"] != "peekaboo"}
actual = {item["identity"]: item["state"] for item in json.loads((root / "Package.resolved").read_text())["pins"]}
assert actual == expected, "transitive package lock changed"
print("native source absorption and complete transitive pins verified")
