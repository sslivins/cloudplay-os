"""Keep Plymouth visible during DHCP, then release DRM before the user session."""
import json
import signal
import subprocess
import threading
import time

import readiness


def progress(message):
    print("Cloudplay boot: " + message, flush=True)
    try:
        subprocess.run(["/usr/bin/plymouth", "display-message", "--text=" + message],
                       timeout=1, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main():
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.set())
    mode = readiness.wait_for_mode(stopping, use_cache=False, progress=progress)
    if mode and not stopping.is_set():
        path = readiness.DECISION
        staged = path.with_suffix(".new")
        staged.write_text(json.dumps({"mode": mode, "at": time.monotonic()}) + "\n")
        staged.chmod(0o644)
        staged.replace(path)
        stopping.wait(0.7)


if __name__ == "__main__":
    main()
