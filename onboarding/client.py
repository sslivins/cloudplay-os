#!/usr/bin/env python3
"""Use a separate ephemeral, sandboxed setup browser before the persistent GFN one."""
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
import readiness

URL = "http://127.0.0.1:8765"


def ready():
    state = readiness.status(URL)
    if state and state["connected"]:
        return True
    network = readiness.network_snapshot()
    return bool(network and network["connected"])


def main():
    import pwd
    if os.getuid() != 1000 or os.geteuid() != 1000 or pwd.getpwuid(os.getuid()).pw_name != "cloudplay":
        raise SystemExit("Run inside the cloudplay user session")
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    if runtime.stat().st_uid != os.getuid() or not os.environ.get("WAYLAND_DISPLAY"):
        raise SystemExit("Missing owned Wayland runtime")
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.set())
    mode = readiness.wait_for_mode(stopping)
    profile = runtime / "cloudplay-network-profile"
    child = None
    try:
        if mode == "setup" and not stopping.is_set():
            if profile.is_symlink():
                raise SystemExit("Setup profile must not be a symlink")
            if profile.exists():
                shutil.rmtree(profile)
            profile.mkdir(mode=0o700)
            print("Cloudplay startup: launching temporary network-setup browser", flush=True)
            child = subprocess.Popen([
                "/usr/bin/chromium", "--ozone-platform=wayland", "--use-angle=gles",
                "--kiosk", "--no-first-run", "--no-default-browser-check",
                "--password-store=basic", "--disable-sync", "--disable-extensions",
                "--user-data-dir=" + str(profile), URL + "/",
            ], start_new_session=True)
            while child.poll() is None and not stopping.is_set():
                if ready():
                    break
                stopping.wait(0.25)
            if not stopping.is_set() and child.poll() is not None and not ready():
                print(f"Cloudplay startup: setup browser exited {child.returncode} before network readiness",
                      flush=True)
                raise SystemExit(1)  # The existing supervisor handles bounded restart/backoff.
    finally:
        if child:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                # Finish before the outer supervisor's five-second termination deadline.
                child.wait(timeout=1)
                time.sleep(0.2)
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            shutil.rmtree(profile, ignore_errors=True)
    if not stopping.is_set():
        print("Cloudplay startup: launching persistent GeForce NOW browser", flush=True)
        os.execv("/usr/local/bin/cloudplay-start", ["cloudplay-start"])


if __name__ == "__main__":
    main()
