#!/usr/bin/env python3
"""Refuse experimental builds without reviewed hardware policy and real trust roots."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from updater.artifacts import SemVer


def validate_approval(value):
    if value.get("schema") != 1 or value.get("experimental_build_approved") is not True:
        raise ValueError("HARDWARE_POLICY: explicit experimental build approval required")
    if value.get("production_approved") is not False:
        raise ValueError("HARDWARE_POLICY: this path cannot approve a production baseline")
    if value.get("physical_acceptance") not in ("unknown", "incomplete", "passed"):
        raise ValueError("HARDWARE_POLICY: physical acceptance must be explicitly recorded")
    if not value.get("evidence") or not value.get("reviewed_by"):
        raise ValueError("HARDWARE_POLICY: reviewer and evidence/reference required")
    platforms = value.get("platforms", {})
    if not platforms or not set(platforms) <= {"pi5", "cm5"}:
        raise ValueError("HARDWARE_POLICY: explicit pi5/cm5 prerequisite policy required")
    for policy in platforms.values():
        if not isinstance(policy, dict) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", policy.get("minimum_eeprom", "")):
            raise ValueError("HARDWARE_POLICY: minimum EEPROM date required")
        if not re.fullmatch(r"0x[0-9a-fA-F]+", policy.get("boot_order", "")):
            raise ValueError("HARDWARE_POLICY: exact BOOT_ORDER required")
        if policy.get("tryboot_required") is not True:
            raise ValueError("HARDWARE_POLICY: tryboot prerequisite required")


def main():
    if os.environ.get("CLOUDPLAY_OTA_EXPERIMENTAL", "0") != "1":
        raise ValueError("OPT_IN: CLOUDPLAY_OTA_EXPERIMENTAL=1 required")
    required = ("CLOUDPLAY_OTA_VERSION", "CLOUDPLAY_OTA_MINIMUM_VERSION",
                "CLOUDPLAY_OTA_PLATFORM", "CLOUDPLAY_OTA_KEY_EPOCH",
                "CLOUDPLAY_OTA_SECRET_KEY", "CLOUDPLAY_OTA_WORKFLOW")
    for key in required:
        if not os.environ.get(key):
            raise ValueError(f"CONFIG: missing {key}")
    version = SemVer.parse(os.environ["CLOUDPLAY_OTA_VERSION"])
    minimum = SemVer.parse(os.environ["CLOUDPLAY_OTA_MINIMUM_VERSION"])
    if not version.prerelease or not minimum < version:
        raise ValueError("VERSION: experimental builds require a newer prerelease SemVer")
    epoch = int(os.environ["CLOUDPLAY_OTA_KEY_EPOCH"])
    if epoch < 1:
        raise ValueError("KEYS: invalid epoch")
    approval = REPO / "image-build/hardware-approval.json"
    validate_approval(json.loads(approval.read_text()))
    if os.environ["CLOUDPLAY_OTA_PLATFORM"] not in json.loads(approval.read_text())["platforms"]:
        raise ValueError("HARDWARE_POLICY: selected platform has no reviewed prerequisites")
    keys = REPO / "image-build/keys"
    for name in (f"epoch-{epoch}-primary.pub", f"epoch-{epoch}-recovery.pub"):
        key = keys / name
        if key.is_symlink() or not key.is_file() or not 20 <= key.stat().st_size <= 8192:
            raise ValueError("KEYS: real primary AND separately held recovery public keys required")
    tracked = subprocess.check_output(
        ["git", "-C", str(REPO), "ls-files", "--", "image-build/keys/*.pub",
         "image-build/hardware-approval.json"], text=True, timeout=30).splitlines()
    for path in [approval, *keys.glob("*.pub")]:
        if path.relative_to(REPO).as_posix() not in tracked:
            raise ValueError("KEYS: hardware approval and public keys must be committed")
    primary = (keys / f"epoch-{epoch}-primary.pub").read_text().splitlines()[-1]
    recovery = (keys / f"epoch-{epoch}-recovery.pub").read_text().splitlines()[-1]
    if primary == recovery:
        raise ValueError("KEYS: primary and recovery must be independent")
    secret = Path(os.environ["CLOUDPLAY_OTA_SECRET_KEY"])
    if secret.is_symlink() or not secret.is_file() or secret.resolve().is_relative_to(REPO):
        raise ValueError("KEYS: external regular private-key file required")
    if secret.stat().st_mode & 0o077:
        raise ValueError("KEYS: private key must be owner-only")
    print("Experimental prerequisite configuration present; physical acceptance is NOT attested.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
