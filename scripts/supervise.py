#!/usr/bin/env python3
"""Restart only this kiosk's child process group with a bounded failure rate."""
import os
import signal
import subprocess
import sys
import threading
import time


def next_retry(failures, uptime):
    if uptime >= 120:
        return 0, 2
    failures += 1
    if failures >= 6:
        return 0, 300
    return failures, min(2 ** failures, 60)


def main(command):
    if not command or os.geteuid() == 0:
        raise SystemExit("A nonroot kiosk command is required")
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.set())
    failures = 0
    while not stopping.is_set():
        start = time.monotonic()
        child = subprocess.Popen(command, start_new_session=True)
        while child.poll() is None and not stopping.wait(0.25):
            pass
        # Never terminate a compositor/browser outside this owned process group.
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
            time.sleep(0.2)
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        if stopping.is_set():
            break
        failures, delay = next_retry(failures, time.monotonic() - start)
        print(f"Cloudplay child exited {child.returncode}; retry in {delay}s", flush=True)
        stopping.wait(delay)


if __name__ == "__main__":
    main(sys.argv[1:])
