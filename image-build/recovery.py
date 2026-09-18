#!/usr/bin/env python3
"""Fail visibly without guessing a disk, boot slot, or update policy."""
import os
import subprocess
import sys

MESSAGE = (
    "Cloudplay OS needs local recovery. An update safety check failed. "
    "Do not erase the recovery drive. Boot recovery media and inspect "
    "journalctl -b -u 'cloudplay-*'."
)


def main():
    fd = os.open("/run/cloudplay-update-recovery",
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (MESSAGE + "\n").encode("ascii"))
    finally:
        os.close(fd)
    print(MESSAGE, file=sys.stderr, flush=True)
    result = subprocess.run(["systemctl", "--no-block", "stop", "greetd.service"],
                            check=False, timeout=10, capture_output=True, text=True)
    if result.returncode:
        print("Could not stop kiosk: " + result.stderr.strip(), file=sys.stderr)
    result = subprocess.run(["plymouth", "display-message", "--text=" + MESSAGE],
                            check=False, timeout=5, capture_output=True, text=True)
    if result.returncode:
        print("Plymouth recovery message unavailable: " + result.stderr.strip(), file=sys.stderr)
    try:
        fd = os.open("/dev/console", os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            os.write(fd, ("\n" + MESSAGE + "\n").encode("ascii"))
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"Console recovery message unavailable: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
