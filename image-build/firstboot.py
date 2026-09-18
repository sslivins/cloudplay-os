#!/usr/bin/env python3
"""Prepare same-disk persistent data before networking or the kiosk can start."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from layout import ALIGN_BYTES, MIN_CARD_BYTES, validate_geometry


def run(*args, timeout=120, accepted=(0,), **kwargs):
    result = subprocess.run(list(map(str, args)), timeout=timeout, check=False,
                            stdin=subprocess.DEVNULL, **kwargs)
    if result.returncode not in accepted:
        raise RuntimeError(f"FIRSTBOOT: {args[0]} failed ({result.returncode})")
    return result


def output(*args):
    return run(*args, capture_output=True, text=True).stdout.strip()


def identity(table, root_device, boot_device, disk_bytes):
    parts = validate_geometry(table, growing=True)
    if disk_bytes < MIN_CARD_BYTES:
        raise ValueError("FIRSTBOOT: card is below the supported usable capacity")
    for slot, root_index, boot_index in (("A", 3, 1), ("B", 4, 2)):
        if parts[root_index]["node"] == root_device:
            if parts[boot_index]["node"] != boot_device:
                raise ValueError("FIRSTBOOT: root/boot slot disagreement")
            return slot, parts
    raise ValueError("FIRSTBOOT: running root is not an A/B slot on this disk")


def validate_saved_layout(path, table, parts):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
            info.st_mode & 0o022 or info.st_size > 4096):
        raise ValueError("FIRSTBOOT: unsafe persistent layout record")
    saved = json.loads(path.read_text())
    if saved != {"schema": 1, "disk_id": table["id"],
                 "partitions": {p["name"]: p["uuid"] for p in parts}}:
        raise ValueError("FIRSTBOOT: persistent layout does not match the running disk")


def atomic(path, value, mode=0o600):
    if path.is_symlink():
        raise ValueError("FIRSTBOOT: refusing symlink destination")
    temporary = path.with_name("." + path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                 os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def directory(path, mode=0o700, owner=0):
    if not path.exists():
        path.mkdir(mode=mode)
        os.chown(path, owner, owner)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
        raise ValueError(f"FIRSTBOOT: unsafe persistent directory {path}")
    os.chmod(path, mode)


def mounted_source(path):
    result = run("findmnt", "--noheadings", "--raw", "--output", "SOURCE",
                 "--mountpoint", path, accepted=(0, 1), capture_output=True, text=True)
    if result.returncode == 1:
        return None
    source = result.stdout.strip()
    if not source or "\n" in source:
        raise ValueError("FIRSTBOOT: ambiguous mount")
    return source


def seed_directory(source, target, *, owner=0, mode=0o700):
    if not target.exists():
        temporary = target.with_name("." + target.name + ".seed")
        directory(temporary, mode, owner)
        run("rsync", "-a", "--delete", "--numeric-ids", str(source) + "/",
            str(temporary) + "/", timeout=600)
        os.chown(temporary, owner, owner)
        os.chmod(temporary, mode)
        run("sync", "-f", temporary)
        os.rename(temporary, target)
        run("sync", "-f", target.parent)
    directory(target, mode, owner)


def bind(source, destination):
    if destination.is_symlink():
        raise ValueError("FIRSTBOOT: bind destination must not be a symlink")
    if mounted_source(destination):
        if output("stat", "-Lc", "%d:%i", source) != output("stat", "-Lc", "%d:%i", destination):
            raise ValueError("FIRSTBOOT: existing bind mount has the wrong identity")
        return
    run("mount", "--bind", source, destination)


def filesystem_geometry(device):
    header = run("dumpe2fs", "-h", device, capture_output=True, text=True,
                 env={**os.environ, "LC_ALL": "C"}).stdout
    values = []
    for field in ("Block count", "Block size"):
        matches = re.findall(rf"^{field}:\s+([0-9]+)\s*$", header, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError("FIRSTBOOT: cannot establish data filesystem geometry")
        values.append(int(matches[0]))
    count, size = values
    if count <= 0 or size not in (1024, 2048, 4096, 8192, 16384, 32768, 65536):
        raise ValueError("FIRSTBOOT: invalid data filesystem geometry")
    return count, size


def grow_filesystem(device):
    run("e2fsck", "-p", device, accepted=(0, 1), timeout=600)
    count, size = filesystem_geometry(device)
    capacity = int(output("blockdev", "--getsize64", device))
    target = (capacity // ALIGN_BYTES * ALIGN_BYTES) // size
    if target <= 0 or count * size > capacity:
        raise ValueError("FIRSTBOOT: data filesystem exceeds its partition")
    # Leave a sub-alignment tail alone. Repeated implicit resize requests can
    # differ by a few blocks and require a forced fsck even on a clean device.
    if count >= target:
        return
    run("e2fsck", "-f", "-p", device, accepted=(0, 1), timeout=600)
    run("resize2fs", device, str(target), timeout=600)
    if filesystem_geometry(device) != (target, size):
        raise ValueError("FIRSTBOOT: data filesystem growth readback mismatch")


def prepare():
    if os.geteuid() != 0:
        raise ValueError("FIRSTBOOT: root required")
    root = str(Path(output("findmnt", "-nro", "SOURCE", "--mountpoint", "/")).resolve())
    boot = str(Path(output("findmnt", "-nro", "SOURCE", "--mountpoint",
                           "/boot/firmware")).resolve())
    parent = output("lsblk", "-nro", "PKNAME", root)
    if not re.fullmatch(r"(?:mmcblk\d+|nvme\d+n\d+|sd[a-z]+)", parent):
        raise ValueError("FIRSTBOOT: unsupported or ambiguous boot disk")
    disk = "/dev/" + parent
    table = json.loads(output("sfdisk", "--json", disk))["partitiontable"]
    slot, parts = identity(table, root, boot, int(output("blockdev", "--getsize64", disk)))
    data_device = parts[5]["node"]
    data = Path("/data")
    if data.is_symlink() or not data.is_dir():
        raise ValueError("FIRSTBOOT: missing real /data mountpoint")
    already_mounted = mounted_source(data)
    if already_mounted:
        if str(Path(already_mounted).resolve()) != data_device:
            raise ValueError("FIRSTBOOT: /data belongs to a different filesystem")
    else:
        if any(data.iterdir()):
            raise ValueError("FIRSTBOOT: refusing to hide existing /data contents")
        # Authenticate the image's layout record before any partition-table write.
        run("mount", "-t", "ext4", "-o", "ro,noload,nosuid,nodev", data_device, data)
        try:
            validate_saved_layout(data / "cloudplay/layout.json", table, parts)
        finally:
            run("umount", data)
        disk_sectors = int(output("blockdev", "--getsize64", disk)) // 512
        if disk_sectors - (parts[5]["start"] + parts[5]["size"]) > 8192:
            # Only the final data partition grows; fixed slot starts/ends never change.
            run("sgdisk", "--move-second-header", disk)
            run("growpart", disk, "6")
            run("partx", "--update", "--nr", "6", disk)
        changed = json.loads(output("sfdisk", "--json", disk))["partitiontable"]
        _, grown = identity(changed, root, boot, int(output("blockdev", "--getsize64", disk)))
        if table["id"] != changed["id"] or any(
                before["uuid"] != after["uuid"] for before, after in zip(parts, grown)):
            raise ValueError("FIRSTBOOT: partition identity changed during data growth")
        grow_filesystem(data_device)
        run("mount", "-t", "ext4", "-o", "nosuid,nodev,noatime", data_device, data)
    persistent = data / "cloudplay"
    directory(persistent, 0o755)
    validate_saved_layout(persistent / "layout.json", table, parts)
    directory(persistent / "update")
    directory(persistent / "identity")
    directory(persistent / "profiles", 0o755)
    directory(persistent / "network")
    sys.path.insert(0, "/usr/local/lib/cloudplay")
    from updater.state import Config
    policy_file = persistent / "update/config.json"
    if not policy_file.exists():
        Config.load("/etc/cloudplay/updater.json")
        atomic(policy_file, Path("/etc/cloudplay/updater.json").read_bytes())
    policy = Config.load(policy_file)
    if stat.S_IMODE(policy_file.stat().st_mode) != 0o600:
        raise ValueError("FIRSTBOOT: persistent update policy must be private")

    machine = Path("/etc/machine-id")
    shared_machine = persistent / "identity/machine-id"
    current_id = machine.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{32}", current_id) or current_id == "0" * 32:
        raise ValueError("FIRSTBOOT: systemd did not initialize a machine identity")
    if not shared_machine.exists():
        atomic(shared_machine, (current_id + "\n").encode(), 0o644)
    elif shared_machine.is_symlink() or shared_machine.read_text().strip() != current_id:
        raise ValueError("FIRSTBOOT: machine identity differs; refuse network startup")

    ssh_keys = persistent / "identity/ssh"
    directory(ssh_keys)
    run("ssh-keygen", "-A")
    for kind in ("rsa", "ecdsa", "ed25519"):
        for suffix in ("", ".pub"):
            name = "ssh_host_" + kind + "_key" + suffix
            destination = Path("/etc/ssh") / name
            shared = ssh_keys / name
            if not shared.exists():
                atomic(shared, destination.read_bytes(), 0o644 if suffix else 0o600)
            if shared.is_symlink() or not shared.is_file():
                raise ValueError("FIRSTBOOT: invalid persistent SSH identity")
            bind(shared, destination)

    for source, target in (
            (Path("/etc/NetworkManager/system-connections"), persistent / "network/connections"),
            (Path("/var/lib/cloudplay-network"), persistent / "network/onboarding")):
        directory(source)
        seed_directory(source, target)
        bind(target, source)
    home = Path("/home/cloudplay/.config/cloudplay")
    for target_slot in ("A", "B"):
        seed_directory(home, persistent / "profiles" / target_slot, owner=policy.browser_uid)
    bind(persistent / "profiles" / slot, home)
    run("sync", "-f", data)
    print(json.dumps({"schema": 1, "slot": slot, "disk": disk, "persistent_data": "ready"}))


if __name__ == "__main__":
    try:
        prepare()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"Cloudplay persistent-data initialization failed: {exc}")
