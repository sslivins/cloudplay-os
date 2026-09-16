#!/usr/bin/env python3
"""Local administrator install, not an authenticated OTA service."""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import uuid
from pathlib import Path, PurePosixPath

MAX_BYTES = 32 * 1024 * 1024


def unpack(archive, target, commit, digest):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Full reviewed commit required")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Archive SHA256 required")
    if archive.stat().st_size > MAX_BYTES:
        raise ValueError("Archive too large")
    data = archive.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError("Archive too large")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Archive SHA256 mismatch")
    prefix = f"gfn-pi-compat-{commit}"
    total = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if (
                not parts or parts[0] != prefix
                or any(p in (".", "..") for p in parts)
                or "\\" in member.name
                or not (member.isdir() or member.isfile())
                or member.name in seen
            ):
                raise ValueError("Unsafe archive member")
            seen.add(member.name)
            total += member.size
            if total > MAX_BYTES or len(seen) > 4096:
                raise ValueError("Expanded archive too large")
            output = target.joinpath(*parts[1:])
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                if len(parts) < 2:
                    raise ValueError("Invalid archive root")
                output.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, output.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    manifest = json.loads((target / "manifest.json").read_text())
    if manifest.get("manifest_version") != 3:
        raise ValueError("Unpacked root must contain an MV3 manifest")
    return manifest


def install(archive, commit, digest):
    if os.geteuid() != 0:
        raise PermissionError("Run using sudo after independent source/digest review")
    versions = Path("/opt/cloudplay/extensions")
    versions.mkdir(parents=True, exist_ok=True, mode=0o755)
    final = versions / f"{commit}-{digest}"
    if final.exists():
        raise ValueError("Version already installed; use the existing version for rollback")
    stage = versions / f".install-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    link = Path("/opt") / f".gfn-pi-compat-{uuid.uuid4().hex}"
    try:
        unpack(archive, stage, commit, digest)
        (stage / "cloudplay-source.json").write_text(json.dumps({
            "repository": "sslivins/gfn-pi-compat", "commit": commit,
            "archive_sha256": digest,
        }, indent=2) + "\n")
        for item in [stage, *stage.rglob("*")]:
            os.chown(item, 0, 0)
            item.chmod(0o555 if item.is_dir() else 0o444)
        stage.rename(final)
        link.symlink_to(final, target_is_directory=True)
        os.replace(link, "/opt/gfn-pi-compat")
    finally:
        link.unlink(missing_ok=True)
        if stage.exists():
            for item in [stage, *stage.rglob("*")]:
                if item.is_dir():
                    item.chmod(0o700)
            shutil.rmtree(stage)
    print(f"Installed {final}; restart Chromium. Previous version retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("commit")
    parser.add_argument("sha256")
    arguments = parser.parse_args()
    install(arguments.archive.resolve(), arguments.commit, arguments.sha256)
