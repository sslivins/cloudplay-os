#!/usr/bin/env python3
"""Validate source pins and fetch digest-checked inputs; no implicit latest."""
import argparse
import hashlib
import json
import re
import shutil
import urllib.request
from pathlib import Path

COMMIT_UNSET = "UNSET_REQUIRES_REVIEWED_EXTENSION_COMMIT"
HASH_UNSET = "UNSET_REQUIRES_ARCHIVE_SHA256"
ROOT = Path(__file__).resolve().parents[1]


def validate(lock, allow_unset=False):
    def require(value, pattern):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"Invalid pinned value: {value!r}")

    if lock["schema"] != 1:
        raise ValueError("Unsupported manifest schema")
    pi = lock["pi_gen"]
    require(pi["commit"], r"[0-9a-f]{40}")
    if (pi["repository"], pi["release"], pi["architecture"]) != (
        "https://github.com/RPi-Distro/pi-gen.git", "trixie", "arm64"
    ):
        raise ValueError("Unexpected pi-gen target")
    browser = lock["browser"]
    if browser["repository"] != "sslivins/chromium-rpi-hevc":
        raise ValueError("Unexpected browser repository")
    require(browser["release"], r"v[0-9]+\.[0-9]+\.[0-9]+")
    require(browser["version"], r"[0-9A-Za-z.+~-]+")
    require(browser["package_version"], r"(?:[0-9]+:)?[0-9][0-9A-Za-z.+:~\-]*")
    expected = {
        f"{name}_{browser['version']}_{arch}.deb"
        for name, arch in [
            ("chromium", "arm64"), ("chromium-common", "arm64"),
            ("chromium-sandbox", "arm64"), ("chromium-l10n", "all"),
        ]
    }
    assets = browser["assets"]
    if len(assets) != 4 or {a["filename"] for a in assets} != expected:
        raise ValueError("Expected exactly the four pinned Chromium packages")
    for asset in assets:
        require(asset["sha256"], r"[0-9a-f]{64}")
    ext = lock["extension"]
    if ext["repository"] != "sslivins/gfn-pi-compat":
        raise ValueError("Unexpected extension repository")
    if allow_unset and (ext["commit"], ext["sha256"]) == (COMMIT_UNSET, HASH_UNSET):
        return
    require(ext["commit"], r"[0-9a-f]{40}")
    require(ext["sha256"], r"[0-9a-f]{64}")


def verify(path, digest):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != digest:
        raise ValueError(f"SHA256 mismatch: {Path(path).name}")


def download(url, destination, digest):
    if destination.exists():
        verify(destination, digest)
        return
    partial = destination.with_name(destination.name + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=90) as source, partial.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        verify(partial, digest)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def stage(lock, source, destination):
    validate(lock)
    inputs = [(asset["filename"], asset["sha256"]) for asset in lock["browser"]["assets"]]
    inputs.append(("extension.tar.gz", lock["extension"]["sha256"]))
    for name, digest in inputs:
        verify(source / name, digest)
    destination.mkdir(parents=True, exist_ok=False)
    for name, _ in inputs:
        shutil.copyfile(source / name, destination / name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "fetch", "stage"])
    parser.add_argument("--allow-unset", action="store_true")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if (args.command == "stage") != (args.destination is not None):
        parser.error("--destination is required only for stage")
    lock = json.loads((ROOT / "manifest.json").read_text())
    validate(lock, allow_unset=args.allow_unset and args.command == "validate")
    if args.command == "fetch":
        dest = ROOT / "build" / "artifacts"
        dest.mkdir(parents=True, exist_ok=True)
        browser = lock["browser"]
        base = f"https://github.com/{browser['repository']}/releases/download/{browser['release']}"
        for asset in browser["assets"]:
            download(f"{base}/{asset['filename']}", dest / asset["filename"], asset["sha256"])
        ext = lock["extension"]
        download(
            f"https://codeload.github.com/{ext['repository']}/tar.gz/{ext['commit']}",
            dest / "extension.tar.gz", ext["sha256"],
        )
    elif args.command == "stage":
        stage(lock, ROOT / "build" / "artifacts", args.destination)
    print("Manifest valid" + (" (unfinalized pins permitted)" if args.allow_unset else ""))


if __name__ == "__main__":
    main()
