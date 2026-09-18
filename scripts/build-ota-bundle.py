#!/usr/bin/env python3
"""Build immutable, signed OTA assets from the exact flash-image boot/root inputs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from updater import artifacts  # noqa: E402


def inventory(boot: Path, root: Path):
    manifest, sources = {}, {}
    for prefix, base in (("boot", boot), ("root", root)):
        if base.is_symlink() or not base.is_dir():
            raise artifacts.ArtifactError("BUILD", "inputs must be real directories")
        pending = [(prefix, base)]
        while pending:
            name, path = pending.pop()
            artifacts._path(name)
            if name in artifacts.GENERATED_PATHS:
                if path.is_dir() and not path.is_symlink():
                    raise artifacts.ArtifactError("BUILD", "generated file is a directory")
                continue
            info = path.lstat()
            record = dict(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
            extended = artifacts.read_xattrs(path)
            if extended:
                record["xattrs"] = extended
            if stat.S_ISDIR(info.st_mode):
                record["type"] = "directory"
                pending.extend((name + "/" + p.name, p) for p in sorted(path.iterdir(), reverse=True))
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                artifacts._link(name, target)
                record.update(type="symlink", target=target)
            elif stat.S_ISREG(info.st_mode):
                # Hardlinked source files become independent regular archive members.
                record.update(type="file", size=info.st_size, sha256=artifacts.sha256_file(path))
            else:
                raise artifacts.ArtifactError("BUILD", f"unsupported input member: {name}")
            manifest[name], sources[name] = record, path
            if len(manifest) > artifacts.MAX_MEMBERS:
                raise artifacts.ArtifactError("LIMIT", "too many input files")
    return dict(sorted(manifest.items())), sources


def write_tar(path: Path, metadata: dict, sources: dict):
    data = artifacts.canonical_json(metadata)
    if len(data) > artifacts.MAX_METADATA:
        raise artifacts.ArtifactError("LIMIT", "metadata is too large")
    with path.open("xb") as output, tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
        header = tarfile.TarInfo("meta.json")
        header.size, header.mode = len(data), 0o644
        tar.addfile(header, io.BytesIO(data))
        for name, record in metadata["manifest"].items():
            header = tarfile.TarInfo(name)
            header.mode, header.uid, header.gid = record["mode"], record["uid"], record["gid"]
            if record["type"] == "directory":
                header.type = tarfile.DIRTYPE
                tar.addfile(header)
            elif record["type"] == "symlink":
                header.type, header.linkname = tarfile.SYMTYPE, record["target"]
                tar.addfile(header)
            else:
                header.size = record["size"]
                with sources[name].open("rb") as source:
                    tar.addfile(header, source)


def build(args) -> dict:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    repository = Path(__file__).resolve().parents[1]
    if args.secret_key.resolve().is_relative_to(repository):
        raise artifacts.ArtifactError("BUILD", "private signing keys must be outside the repository")
    match = artifacts._KEY.fullmatch(args.public_key.name)
    if not match or int(match[1]) != args.key_epoch:
        raise artifacts.ArtifactError("BUILD", "public key must be named epoch-N-name.pub")
    version = artifacts.SemVer.parse(args.version)
    if args.channel == "stable" and version.prerelease:
        raise artifacts.ArtifactError("BUILD", "stable versions cannot be prereleases")
    name = f"cloudplay-os-{args.version}-{args.platform}.tar.zst"
    if not all(c.isalnum() or c in ".-+" for c in args.platform) or len(args.platform) > 64:
        raise artifacts.ArtifactError("BUILD", "invalid platform name")
    bundle = output / name
    signature = output / (name + ".minisig")
    catalog_path = output / (name + ".catalog.json")
    catalog_sig = output / (name + ".catalog.json.minisig")
    paths = (bundle, signature, catalog_path, catalog_sig)
    if any(p.exists() or p.is_symlink() for p in paths):
        raise artifacts.ArtifactError("IMMUTABLE", "refusing to overwrite existing release assets")
    work = output / (".build-ota-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    created = []
    try:
        manifest, sources = inventory(args.boot, args.root)
        meta = dict(
            schema=1, version=args.version, source_commit=args.source_commit,
            minimum_source_version=args.minimum_source_version, platform=args.platform,
            channel=args.channel, key_epoch=args.key_epoch, created_at=args.created_at,
            workflow=args.workflow, data_schema_min=args.data_schema_min,
            data_schema_max=args.data_schema_max, manifest=manifest,
            manifest_sha256=hashlib.sha256(artifacts.canonical_json(manifest)).hexdigest(),
            payload_bytes=sum(r.get("size", 0) for r in manifest.values()))
        constraints = dict(platform=args.platform, channel=args.channel,
                           current_version=args.minimum_source_version,
                           highest_version=args.minimum_source_version,
                           minimum_key_epoch=args.key_epoch, data_schema=args.data_schema_min)
        artifacts.validate_metadata(meta, **constraints)
        archive = work / "payload.tar"
        write_tar(archive, meta, sources)
        artifacts._scan_tar(archive)
        if archive.stat().st_size > artifacts.MAX_EXPANDED:
            raise artifacts.ArtifactError("LIMIT", "archive is too large")
        with bundle.open("xb") as destination:
            created.append(bundle)
            subprocess.run(["zstd", "-T1", "-10", "--stdout", "--no-progress",
                            "--zstd=wlog=27", str(archive)], stdout=destination,
                           stdin=subprocess.DEVNULL, check=True, timeout=1200)
            destination.flush()
            os.fsync(destination.fileno())
        compressed_size = artifacts._regular(bundle, artifacts.MAX_COMPRESSED)
        catalog = {k: meta[k] for k in
                   ("schema", "version", "source_commit", "platform", "channel", "key_epoch")}
        catalog.update(name=name, compressed_size=compressed_size,
                       uncompressed_size=archive.stat().st_size,
                       sha256=artifacts.sha256_file(bundle),
                       required_staging_bytes=archive.stat().st_size * 2 + compressed_size + 64 * 1024**2)
        with catalog_path.open("xb") as destination:
            created.append(catalog_path)
            destination.write(artifacts.canonical_json(catalog))
            destination.flush()
            os.fsync(destination.fileno())
        for message, sig in ((bundle, signature), (catalog_path, catalog_sig)):
            # Reserve the name exclusively; minisign may replace only our own reservation.
            with sig.open("xb"):
                created.append(sig)
            subprocess.run(["minisign", "-Sm", str(message), "-s", str(args.secret_key),
                            "-x", str(sig)], stdin=subprocess.DEVNULL, check=True, timeout=60)
            if not artifacts._verify_signature(message, sig, args.public_key):
                raise artifacts.ArtifactError("SIGNATURE", "fresh signature did not match public key")
        # Verify actual compressed output, extraction and hashes, not just source inventory.
        keys = work / "keys"
        keys.mkdir(mode=0o700)
        shutil.copyfile(args.public_key, keys / args.public_key.name)
        verified = artifacts.verify_bundle(bundle, signature, keys, work / "verified", **constraints)
        if verified != meta:
            raise artifacts.ArtifactError("BUILD", "round-trip metadata mismatch")
        return catalog
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("boot", "root", "output", "secret-key", "public-key"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("version", "source-commit", "minimum-source-version", "platform", "workflow"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    parser.add_argument("--key-epoch", type=int, required=True)
    parser.add_argument("--data-schema-min", type=int, default=1)
    parser.add_argument("--data-schema-max", type=int, default=1)
    parser.add_argument("--created-at", default=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    args = parser.parse_args()
    try:
        print(artifacts.canonical_json(build(args)).decode())
    except (artifacts.ArtifactError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"OTA build failed: {exc}\n")


if __name__ == "__main__":
    main()
