#!/usr/bin/env python3
"""Native updater UI on a dedicated UID and compositor; no browser subprocess."""
import os
from pathlib import Path
import pwd
import stat

from gamepad import Gamepads
from heartbeat import Heartbeats
from host import Control
from main import run
from updates import Updates


class NoBrowser:
    service = None

    def stop(self):
        pass

    def exited(self):
        return False

    def start(self, _):
        raise RuntimeError("Browsers cannot run in the trusted update session")


def main():
    if (os.getuid() != 450 or os.geteuid() != 450
            or pwd.getpwuid(450).pw_name != "cloudplay-update"):
        raise SystemExit("Dedicated update identity required")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    info = runtime.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 450
            or stat.S_IMODE(info.st_mode) != 0o700 or not os.environ.get("WAYLAND_DISPLAY")):
        raise SystemExit("Private trusted compositor runtime required")
    os.umask(0o077)
    run(NoBrowser(), Control(runtime), Gamepads(), Updates(), trusted_updates=True,
        heartbeats=Heartbeats(runtime))


if __name__ == "__main__":
    main()
