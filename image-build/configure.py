#!/usr/bin/env python3
"""Render a mutation-disabled experimental runtime configuration."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from updater.state import Config


def configuration(approval, platform, epoch):
    policy = approval["platforms"][platform]
    value = dict(
        schema=1, experimental_hardware_validation=False,
        hardware_evidence=str(approval["evidence"]),
        launcher_isolation_verified=False,
        isolation_evidence="",
        launcher_uid=450, browser_uid=1000, socket_gid=450,
        platform=platform, channel="beta", minimum_key_epoch=epoch,
        minimum_eeprom=policy["minimum_eeprom"], boot_order=policy["boot_order"])
    Config(**value).validate()
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = configuration(
        json.loads((REPO / "image-build/hardware-approval.json").read_text()),
        os.environ["CLOUDPLAY_OTA_PLATFORM"], int(os.environ["CLOUDPLAY_OTA_KEY_EPOCH"]))
    with args.output.open("x") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
    release = dict(
        version=os.environ["CLOUDPLAY_OTA_VERSION"],
        source_commit=subprocess.check_output(
            ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "rev-parse", "HEAD"],
            text=True, timeout=30).strip(),
        launcher_smoke_passed=False)
    with args.output.with_name("ota-release.json").open("x") as output:
        json.dump(release, output, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    main()
