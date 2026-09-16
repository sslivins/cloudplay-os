#!/usr/bin/env python3
"""Executed in the final export rootfs after kiosk and splash verification."""
import hashlib
import json
import subprocess
from pathlib import Path

base = Path("/opt/cloudplay-build-inputs")
output = Path("/usr/local/share/cloudplay")
output.mkdir(parents=True, exist_ok=True)
packages = subprocess.check_output([
    "dpkg-query", "-W",
    "-f=${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\\n",
], text=True)
(output / "packages.tsv").write_text(packages)
sources = {}
for path in [Path("/etc/apt/sources.list"), *sorted(Path("/etc/apt/sources.list.d").glob("*"))]:
    if path.is_file():
        sources[str(path)] = path.read_text()
extension = Path("/opt/gfn-pi-compat")
files = {
    str(path.relative_to(extension)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(extension.rglob("*")) if path.is_file()
}
manifest = {
    "source_pins": json.loads((base / "manifest.json").read_text()),
    "cloudplay_commit": (base / "cloudplay-commit.txt").read_text().strip(),
    "working_tree_patch_sha256": hashlib.sha256((base / "cloudplay-working-tree.patch").read_bytes()).hexdigest(),
    "builder_os_release": (base / "builder-os-release").read_text(),
    "builder_packages_sha256": hashlib.sha256((base / "builder-packages.tsv").read_bytes()).hexdigest(),
    "os_release": Path("/etc/os-release").read_text(),
    "apt_sources": sources,
    "packages_sha256": hashlib.sha256(packages.encode()).hexdigest(),
    "extension_files_sha256": files,
    "kiosk_configuration": json.loads((output / "kiosk-verification.json").read_text()),
    "hdr_prerequisite_diagnostic": json.loads(subprocess.check_output([
        "/usr/local/bin/cloudplay-hdr-check",
    ], text=True)),
    "reproducibility": "pi-gen, browser and extension pinned; OS apt repositories are not snapshot-pinned",
}
(output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
