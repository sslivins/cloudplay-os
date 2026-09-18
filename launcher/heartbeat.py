"""Trusted UI heartbeat plus a bounded real Wayland compositor round trip."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


def probe():
    library = ctypes.CDLL("libwayland-client.so.0")
    library.wl_display_connect.argtypes = [ctypes.c_char_p]
    library.wl_display_connect.restype = ctypes.c_void_p
    library.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
    library.wl_display_roundtrip.restype = ctypes.c_int
    library.wl_display_disconnect.argtypes = [ctypes.c_void_p]
    display = library.wl_display_connect(None)
    if not display:
        raise RuntimeError("Cannot connect to trusted compositor")
    try:
        if library.wl_display_roundtrip(display) < 0:
            raise RuntimeError("Trusted compositor round trip failed")
    finally:
        library.wl_display_disconnect(display)


class Heartbeats:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.stop = threading.Event()
        self.next_tick = 0
        self.worker = threading.Thread(target=self._compositor, daemon=True)
        self.worker.start()

    def write(self, name):
        value = {"boot_id": self.boot_id, "monotonic": time.monotonic()}
        target = self.directory / (name + "-heartbeat.json")
        temporary = target.with_suffix(".new")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
        os.replace(temporary, target)

    def tick(self):
        if time.monotonic() >= self.next_tick:
            self.write("launcher")
            self.next_tick = time.monotonic() + 2

    def _compositor(self):
        while not self.stop.is_set():
            try:
                subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "probe"],
                               timeout=3, check=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.write("compositor")
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"Compositor heartbeat failed: {exc}", file=sys.stderr)
            self.stop.wait(2)

    def close(self):
        self.stop.set()


if __name__ == "__main__":
    if sys.argv[1:] != ["probe"]:
        raise SystemExit("Only the fixed compositor probe is supported")
    probe()
