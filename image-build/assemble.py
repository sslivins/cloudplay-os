#!/usr/bin/env python3
"""Assemble experimental GPT images. Only newly created regular image files are writable."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime, timezone

from layout import IMAGE_BYTES, MIB, PARTITIONS, AUTOBOOT, cmdline, fstab, validate_geometry

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from updater.artifacts import (GENERATED_PATHS, _set_attributes, canonical_json,
                               sha256_file, verify_attributes)
from updater.platform import safe_child


def run(*args, **kwargs):
    kwargs.setdefault("timeout", 1800)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True, timeout=60,
                                   env={**os.environ, "LC_ALL": "C"}).strip()


def create_image(path):
    if path.is_symlink() or path.exists():
        raise ValueError("IMAGE_ONLY: output must not exist (devices and existing images refused)")
    if path.parent.resolve() != path.parent.absolute() or not path.parent.is_dir():
        raise ValueError("IMAGE_ONLY: output parent must be a real existing directory")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("IMAGE_ONLY: not a regular image file")
        os.ftruncate(descriptor, IMAGE_BYTES)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def assert_loop(loop, image):
    if not re.fullmatch(r"/dev/loop\d+", loop):
        raise ValueError("IMAGE_ONLY: not a loop device")
    records = json.loads(output("losetup", "--json", "--list", "--output", "NAME,BACK-FILE", loop))["loopdevices"]
    if len(records) != 1 or records[0]["name"] != loop:
        raise ValueError("IMAGE_ONLY: ambiguous loop association")
    backing = Path(records[0]["back-file"])
    if not backing.is_file() or not os.path.samefile(backing, image):
        raise ValueError("IMAGE_ONLY: loop is not backed by our new image")


def validate_inputs(boot, root):
    for name in GENERATED_PATHS:
        if name.startswith("root/etc/ssh/"):
            identity = safe_child(root, name.split("/", 1)[1])
            if identity.exists():
                raise ValueError("IDENTITY: image source contains a shared SSH host key")
    command_template = safe_child(boot, "cmdline.txt")
    if (not stat.S_ISREG(command_template.lstat().st_mode)
            or command_template.stat().st_size > 4096):
        raise ValueError("GENERATED: invalid boot command-line template")
    safe_child(root, "etc/fstab")
    safe_child(root, "etc/machine-id")
    return command_template.read_text(encoding="ascii")


def validate_release(root, version, source_commit):
    record = safe_child(root, "usr/share/cloudplay/release.json")
    if not stat.S_ISREG(record.lstat().st_mode) or record.stat().st_size > 4096:
        raise ValueError("SOURCE: invalid payload release identity")
    release = json.loads(record.read_text())
    if (release.get("version") != version or release.get("source_commit") != source_commit
            or release.get("launcher_smoke_passed") is not True):
        raise ValueError("SOURCE: payload version/source or launcher smoke does not match")


def write_fat(device, source, readback):
    """Use userspace FAT IO; image builders need no host vfat kernel module."""
    children = sorted(source.iterdir())
    if not children:
        raise ValueError("FAT: refusing empty boot filesystem")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("FAT: symlink source is not representable")
    run("mcopy", "-s", "-p", "-i", device, *children, "::/")
    readback.mkdir(mode=0o755)
    run("mcopy", "-s", "-i", device, "::*", readback)
    for path in (readback, *readback.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o644)
    listing = output("mdir", "-i", device, "::")
    match = re.search(r"(?m)^\s*([\d ,]+)\s+bytes free\s*$", listing)
    if not match:
        raise ValueError("FAT: cannot verify remaining filesystem capacity")
    free = int(re.sub(r"\D", "", match[1]))
    return free


def build():
    if os.environ.get("CLOUDPLAY_OTA_EXPERIMENTAL") != "1" or os.geteuid() != 0:
        raise ValueError("OPT_IN: experimental root-only image assembly")
    deploy = (REPO / "build/pi-gen/deploy").resolve()
    inputs = deploy / "ota-inputs"
    release = deploy / "experimental-ota"
    release.mkdir(mode=0o755)
    boot, root = inputs / "boot", inputs / "root"
    source_cmdline = validate_inputs(boot, root)
    spec = importlib.util.spec_from_file_location("bundle_builder", REPO / "scripts/build-ota-bundle.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    env = os.environ
    args = SimpleNamespace(
        boot=boot, root=root, output=release,
        version=env["CLOUDPLAY_OTA_VERSION"],
        minimum_source_version=env["CLOUDPLAY_OTA_MINIMUM_VERSION"],
        source_commit=output("git", "-c", f"safe.directory={REPO}", "-C", REPO, "rev-parse", "HEAD"),
        platform=env["CLOUDPLAY_OTA_PLATFORM"], channel="beta",
        key_epoch=int(env["CLOUDPLAY_OTA_KEY_EPOCH"]),
        workflow=env["CLOUDPLAY_OTA_WORKFLOW"], data_schema_min=1, data_schema_max=1,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        secret_key=Path(env["CLOUDPLAY_OTA_SECRET_KEY"]),
        public_key=REPO / "image-build/keys" / f'epoch-{env["CLOUDPLAY_OTA_KEY_EPOCH"]}-primary.pub',
    )
    validate_release(root, args.version, args.source_commit)
    catalog, signed_metadata = builder.build(args)
    expected, _ = builder.inventory(boot, root)
    if expected != signed_metadata["manifest"]:
        raise ValueError("IMAGE_MANIFEST: source changed after signed bundle creation")
    manifest_digest = signed_metadata["manifest_sha256"]
    image = release / f"cloudplay-os-{args.version}-{args.platform}-experimental.img"
    create_image(image)
    lines = ["label: gpt", "unit: sectors", "sector-size: 512"]
    for _, name, _, kind, start, size in PARTITIONS:
        partition_type = ("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7" if kind == "vfat"
                          else "0FC63DAF-8483-4772-8E79-3D69D8477DE4")
        lines.append(f'start={start * 2048}, size={size * 2048}, type={partition_type}, name="{name}"')
    run("sfdisk", image, input="\n".join(lines) + "\n", text=True)
    loop, mounts = None, []
    work = inputs / "image-mounts"
    work.mkdir()
    try:
        loop = output("losetup", "--find", "--show", "--partscan", image)
        assert_loop(loop, image)
        run("udevadm", "settle", "--timeout=30")
        table = json.loads(output("sfdisk", "--json", loop))["partitiontable"]
        parts = validate_geometry(table)
        for index, name, label, kind, _, _ in PARTITIONS:
            assert_loop(loop, image)
            device = f"{loop}p{index}"
            if parts[index - 1]["node"] != device or not stat.S_ISBLK(os.stat(device).st_mode):
                raise ValueError("IMAGE_ONLY: unexpected partition device")
            if kind == "vfat":
                run("mkfs.vfat", "-F", "32", "-n", label, device)
            else:
                run("mkfs.ext4", "-q", "-F", "-L", label, "-E",
                    "lazy_itable_init=0,lazy_journal_init=0", device)
            mount = work / name
            mount.mkdir()
            if kind == "ext4":
                run("mount", device, mount)
                mounts.append(mount)
                (mount / "lost+found").rmdir()
            if output("blkid", "-p", "-s", "LABEL", "-o", "value", device) != label:
                raise ValueError("LAYOUT: filesystem label mismatch after format")
        (work / "boot-control/autoboot.txt").write_text(AUTOBOOT)
        (work / "boot-control/config.txt").write_text("# Cloudplay experimental selector\n")
        control_readback = work / "control-readback"
        write_fat(f"{loop}p1", work / "boot-control", control_readback)
        if {p.name: p.read_bytes() for p in control_readback.iterdir()} != {
                p.name: p.read_bytes() for p in (work / "boot-control").iterdir()}:
            raise ValueError("GENERATED: boot-control readback mismatch")
        for slot in ("A", "B"):
            target_boot, target_root = work / f"boot-{slot}", work / f"root-{slot}"
            run("rsync", "-rt", str(boot) + "/", str(target_boot) + "/")
            run("rsync", "-aAX", "--numeric-ids", str(root) + "/", str(target_root) + "/")
            root_uuid = parts[3 if slot == "A" else 4]["uuid"]
            boot_uuid = parts[1 if slot == "A" else 2]["uuid"]
            generated_cmdline = cmdline(source_cmdline, slot, root_uuid)
            generated_fstab = fstab(slot, root_uuid, boot_uuid)
            (target_boot / "cmdline.txt").write_text(generated_cmdline)
            (target_boot / "autoboot.txt").write_text(AUTOBOOT)
            safe_child(target_root, "etc/fstab").write_text(generated_fstab)
            safe_child(target_root, "etc/machine-id").write_text("")
            generated_attributes = dict(type="file", uid=0, gid=0, mode=0o644)
            for name in ("etc/fstab", "etc/machine-id"):
                generated_path = safe_child(target_root, name)
                _set_attributes(generated_path, generated_attributes)
                verify_attributes(generated_path, generated_attributes)
            (target_boot / "slot-valid.json").write_bytes(canonical_json({
                "schema": 1, "slot": slot, "version": args.version,
                "manifest_sha256": manifest_digest,
            }))
            boot_readback = work / f"boot-{slot}-readback"
            boot_free = write_fat(f"{loop}p{2 if slot == 'A' else 3}", target_boot, boot_readback)
            for name in ("cmdline.txt", "autoboot.txt", "slot-valid.json"):
                if (target_boot / name).read_bytes() != (boot_readback / name).read_bytes():
                    raise ValueError(f"GENERATED: FAT {name} readback mismatch")
            actual, _ = builder.inventory(boot_readback, target_root)
            if actual != expected:
                changed = sorted(k for k in actual.keys() | expected.keys()
                                 if actual.get(k) != expected.get(k))
                raise ValueError(f"IMAGE_MANIFEST: mounted slot {slot} differs: {changed[:20]}")
            if (target_root / "etc/fstab").read_text() != generated_fstab:
                raise ValueError("GENERATED: fstab mismatch")
            if (target_boot / "cmdline.txt").read_text() != generated_cmdline:
                raise ValueError("GENERATED: cmdline mismatch")
            if (target_boot / "autoboot.txt").read_text() != AUTOBOOT:
                raise ValueError("GENERATED: autoboot mirror mismatch")
            usage = shutil.disk_usage(target_root)
            if usage.used > usage.total * .75 or boot_free < 1024 * MIB * .25:
                raise ValueError("SPACE: mounted payload exceeds 75% slot budget")
        persistent = work / "data/cloudplay"
        persistent.mkdir(mode=0o755)
        (persistent / "layout.json").write_bytes(canonical_json({
            "schema": 1, "disk_id": table["id"],
            "partitions": {p["name"]: p["uuid"] for p in parts},
        }))
        run("sync")
    finally:
        for mount in reversed(mounts):
            run("umount", mount)
        if loop:
            assert_loop(loop, image)
            run("losetup", "--detach", loop)
    shutil.rmtree(work)
    run("xz", "-T0", "-3", image)
    compressed = Path(str(image) + ".xz")
    compressed.chmod(0o644)
    provenance = {
        "schema": 1, "experimental": True, "production_baseline": False,
        "version": args.version, "source_commit": args.source_commit, "workflow": args.workflow,
        "image": compressed.name, "image_sha256": sha256_file(compressed),
        "bundle": catalog["name"], "bundle_sha256": catalog["sha256"],
        "manifest_sha256": manifest_digest, "geometry_verified": True,
        "both_slot_manifests_verified": True, "physical_acceptance": "unknown",
    }
    (release / "provenance.json").write_bytes(canonical_json(provenance))
    with (release / "SHA256SUMS").open("w") as hashes:
        for path in sorted(release.iterdir()):
            if path.is_file() and path.name != "SHA256SUMS":
                hashes.write(f"{sha256_file(path)}  {path.name}\n")
    shutil.rmtree(inputs)
    print(json.dumps(provenance))


if __name__ == "__main__":
    try:
        build()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"Experimental image build failed: {exc}")
