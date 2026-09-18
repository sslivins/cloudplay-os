#!/usr/bin/env python3
"""Systemd adapter: provision discovery without bypassing physical mutation gates."""
import json
import logging
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, "/usr/local/lib/cloudplay")
from updater.runtime import Runtime
from updater.state import Config, UpdateError


def execute(command, runtime):
    if command == "bootstrap" and Path("/run/cloudplay-update-recovery").exists():
        raise UpdateError("RECOVERY_REQUIRED", "Early boot guard or persistent-data preparation failed")
    if command == "shutdown":
        result = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("Cannot establish system shutdown state")
        if result.stdout.strip() != "stopping":
            return {"graceful_shutdown_recorded": False, "reason": "not-system-shutdown"}
    if command == "bootstrap" and not runtime.journal.path.exists():
        runtime.initialize()
    state = runtime.journal.load()
    try:
        runtime.config.mutation_gate()
    except UpdateError as exc:
        if state.get("pending"):
            raise UpdateError("RECOVERY_REQUIRED", "Pending candidate with disabled safety gates") from exc
        logging.warning("Physical OTA is disabled: %s", exc)
        return {"mutation_disabled": True, "reason": exc.code}
    if command == "bootstrap":
        return runtime.reconcile()
    if command == "deadline":
        return runtime.health(deadline_only=True)
    if command in ("health", "shutdown"):
        return getattr(runtime, command)()
    raise ValueError("Unsupported boot-service operation")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        if len(sys.argv) != 2:
            raise ValueError("One fixed operation required")
        result = execute(sys.argv[1], Runtime(Config.load("/data/cloudplay/update/config.json")))
        print(json.dumps(result, sort_keys=True))
    except (UpdateError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"Cloudplay updater boot service failed: {exc}")
