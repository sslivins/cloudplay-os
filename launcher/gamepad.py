"""Bounded evdev reader, only for udev-selected non-keyboard gamepads."""
import logging
import os
import struct
import time
from pathlib import Path

EVENT = struct.Struct("@llHHi")
SELECT, START = 314, 315
A, B = 304, 305
DPAD = {544: "up", 545: "down", 546: "left", 547: "right"}
MAX_DEVICES = 4
HOLD_SECONDS = 2.0


class Pad:
    def __init__(self):
        self.buttons = set()
        self.axes = {}
        self.since = None
        self.fired = False
        self.sync_lost = False
        self.armed = False

    def event(self, kind, code, value):
        if kind == 0 and code == 3:  # SYN_DROPPED: never infer a held combo.
            self.buttons.clear()
            self.axes.clear()
            self.since = None
            self.fired = False
            self.sync_lost = True
        if self.sync_lost:
            return None
        if kind == 1:
            was_down = code in self.buttons
            if value:
                self.buttons.add(code)
            else:
                self.buttons.discard(code)
            if not {SELECT, START} <= self.buttons:
                self.since = None
                self.fired = False
            if value == 1 and not was_down:
                return {A: "accept", B: "back", **DPAD}.get(code)
        if kind == 3 and code in (16, 17):  # Linux ABS_HAT0X/Y, not uncalibrated sticks.
            previous = self.axes.get(code, 0)
            self.axes[code] = value
            if value and value != previous:
                return ("left" if value < 0 else "right") if code == 16 else (
                    "up" if value < 0 else "down")
        return None

    def held(self, now):
        if not {SELECT, START} <= self.buttons:
            return False
        if self.since is None:
            self.since = now
        if not self.fired and now - self.since >= HOLD_SECONDS:
            self.fired = True
            return True
        return False


def allowed(fd):
    import fcntl
    # EVIOCGBIT(EV_KEY): require gamepad, select/start, reject keyboard keys.
    bits = bytearray(96)
    fcntl.ioctl(fd, 0x80000000 | (len(bits) << 16) | (ord("E") << 8) | 0x21, bits)
    has = lambda code: bool(bits[code // 8] & (1 << (code % 8)))
    return all(has(code) for code in (A, SELECT, START)) and not any(
        has(code) for code in range(1, 256))


class Gamepads:
    def __init__(self, directory=Path("/dev/input")):
        self.directory = directory
        self.devices = {}
        self.next_scan = 0
        self.visible = False
        self.warned = set()

    def warn(self, path, category):
        if path not in self.warned:
            logging.getLogger(__name__).warning(
                "Controller unavailable (%s); keyboard and mouse navigation remain available",
                category)
            self.warned.add(path)

    def close(self):
        for fd, _ in self.devices.values():
            os.close(fd)
        self.devices.clear()

    def poll(self, visible):
        now = time.monotonic()
        if visible != self.visible:
            for _, pad in self.devices.values():
                pad.armed = False
            self.visible = visible
        if now >= self.next_scan:
            self.next_scan = now + 3
            paths = set(sorted(self.directory.glob("cloudplay-gamepad-event*"))[:MAX_DEVICES])
            self.warned.intersection_update(paths)
            for path in list(self.devices):
                if path not in paths:
                    os.close(self.devices.pop(path)[0])
            for path in sorted(paths - self.devices.keys()):
                fd = None
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                    if not allowed(fd):
                        os.close(fd)
                        self.warn(path, "unsupported input capabilities")
                        continue
                    self.devices[path] = (fd, Pad())
                except OSError as error:
                    if fd is not None:
                        os.close(fd)
                    self.warn(path, type(error).__name__)
        actions = []
        for path, (fd, pad) in list(self.devices.items()):
            try:
                try:
                    data = os.read(fd, EVENT.size * 64)
                except BlockingIOError:
                    data = None
                if data == b"":
                    raise OSError("Disconnected")
                if data:
                    for _, _, kind, code, value in EVENT.iter_unpack(data):
                        action = pad.event(kind, code, value)
                        if action and visible and pad.armed:
                            actions.append(action)
                if not pad.buttons.intersection({A, B, *DPAD}) and not any(pad.axes.values()):
                    pad.armed = True
                if pad.sync_lost:
                    raise OSError("Resynchronize by reopening")
                # A full read may leave a queued release unread. Do not infer a
                # deliberate hold while the bounded reader is falling behind.
                saturated = data is not None and len(data) == EVENT.size * 64
                if saturated:
                    pad.since = None
                if not saturated and pad.held(now):
                    # Never activate a button from the same batch that opens the dialog.
                    return ["home"]
            except (OSError, ValueError) as error:
                os.close(fd)
                del self.devices[path]
                self.warn(path, type(error).__name__)
        return actions
