"""Fixed local transition into an isolated, browser-free update session."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import threading
import time

from .client import receive_line
from .platform import run
from .runtime import Runtime, ERRORS
from .state import Config, Journal, UpdateError, atomic_write, fail, trusted_path

SOCKET = Path("/run/cloudplay-maintenance/control.sock")
ACTIVE = SOCKET.parent / "active"
UNIT = "cloudplay-maintenance.service"
LOG = logging.getLogger(__name__)


def kill_gaming_processes(uid, text):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        fail("SESSION", "Race-safe process termination is unavailable")
    pids = text.split()
    if not pids or len(pids) > 1024 or any(not p.isdecimal() or int(p) <= 1 for p in pids):
        fail("SESSION", "Invalid remaining gaming-process identities")
    for value in pids:
        pid = int(value)
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                identities = next((line.split()[1:] for line in status.splitlines()
                                   if line.startswith("Uid:")), None)
                if identities != [str(uid)] * 4:
                    fail("SESSION", "Gaming process identity changed during termination")
                LOG.warning("Terminating leftover gaming process %s after session shutdown", pid)
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                continue
        finally:
            os.close(fd)


def authorize(uid, command, config):
    if command == "open" and uid in (0, config.browser_uid, config.launcher_uid):
        return
    if command == "close" and uid in (0, config.launcher_uid):
        return
    fail("FORBIDDEN", "This identity cannot perform that session transition")


@contextmanager
def provider_lease(config):
    import fcntl
    path = Path(config.socket_path).parent / "provider.lock"
    trusted_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            fail("INTERLOCK", "Provider lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("BUSY", "Close the streaming service before opening update controls") from exc
        yield
    finally:
        os.close(fd)


class Portal:
    def __init__(self, runtime, *, runner=run, marker=ACTIVE):
        self.runtime, self.config = runtime, runtime.config
        self.run, self.marker = runner, Path(marker)
        self.lock = threading.Lock()

    def active(self):
        return self.run(["systemctl", "is-active", UNIT], allowed=(0, 3)).stdout.strip() == "active"

    def transition(self, uid, command):
        authorize(uid, command, self.config)
        if not self.lock.acquire(blocking=False):
            fail("BUSY", "A session transition is already running")
        switching = False
        try:
            if command == "open" and self.active():
                return {"session": "updates"}
            with self.runtime.journal.operation(), provider_lease(self.config):
                if command == "close":
                    if not self.runtime.status().get("provider_launch_allowed"):
                        fail("BUSY", "Finish or cancel the update; a staged update must restart before gaming")
                    self.run(["systemctl", "stop", UNIT], timeout=20)
                    self.marker.unlink(missing_ok=True)
                    self.run(["systemctl", "start", "greetd.service"], timeout=30)
                    return {"session": "gaming"}
                # No maintenance browser runs on the trusted compositor. Terminate
                # the old seat and user manager, not just Chromium's visible tab.
                atomic_write(self.marker, b"trusted-update-session\n")
                switching = True
                self.run(["systemctl", "stop", "greetd.service"], timeout=30)
                result = self.run(["loginctl", "terminate-user", str(self.config.browser_uid)],
                                  timeout=30, allowed=(0, 1))
                if result.returncode:
                    LOG.warning("Gaming user termination reported: %s", result.stderr.strip())
                # logind can leave closing scopes and their user manager alive.
                # Stop their slice, then target verified remaining PIDs without
                # risking PID-reuse signals to an unrelated identity.
                self.run(["systemctl", "--no-block", "stop",
                          f"user-{self.config.browser_uid}.slice"])
                deadline = time.monotonic() + 10
                while True:
                    remaining = self.run(["pgrep", "-u", str(self.config.browser_uid)], allowed=(0, 1))
                    if remaining.returncode == 1:
                        break
                    if time.monotonic() >= deadline:
                        fail("SESSION", "Gaming processes remain; refusing trusted update controls")
                    kill_gaming_processes(self.config.browser_uid, remaining.stdout)
                    time.sleep(.1)
                self.run(["systemctl", "start", UNIT], timeout=30)
                return {"session": "updates"}
        except ERRORS:
            LOG.exception("Secure update session transition failed")
            # Do not silently fall back to gaming after partial isolation.
            if switching:
                self.run(["systemctl", "--no-block", "start", "cloudplay-update-recovery.service"])
            raise
        finally:
            self.lock.release()


def request(command, *, path=SOCKET):
    if command not in ("open", "close"):
        fail("COMMAND", "Unsupported session transition")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(path))
            connection.sendall(json.dumps({"command": command}).encode() + b"\n")
            result = json.loads(receive_line(connection, 4096, timeout=90))
    except (OSError, ValueError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("SESSION", f"Update session unavailable: {exc}") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        fail("SESSION", "Invalid transition response")
    if not result["ok"]:
        error = result["error"]
        raise UpdateError(error["code"], error["message"])
    return result


def _serve(config):
    if os.geteuid() != 0:
        fail("ROOT", "Session broker requires root")
    runtime = Runtime(config)
    portal = Portal(runtime)
    SOCKET.parent.mkdir(mode=0o755, exist_ok=True)
    trusted_path(SOCKET.parent, directory=True)
    if SOCKET.exists() or SOCKET.is_symlink():
        if not stat.S_ISSOCK(SOCKET.lstat().st_mode) or SOCKET.lstat().st_uid != 0:
            fail("SESSION", "Unsafe broker socket")
        SOCKET.unlink()
    stopping = threading.Event()
    slots = threading.BoundedSemaphore(4)
    def handle(connection):
        try:
            _, uid, _ = struct.unpack("3i", connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid not in (0, config.browser_uid, config.launcher_uid):
                fail("FORBIDDEN", "Unauthorized session caller")
            value = json.loads(receive_line(connection, 256, timeout=3))
            if not isinstance(value, dict) or set(value) != {"command"}:
                fail("COMMAND", "One fixed session command required")
            result = {"ok": True, **portal.transition(uid, value["command"])}
        except (*ERRORS, ValueError) as exc:
            result = {"ok": False, "error": {"code": getattr(exc, "code", "SESSION"),
                                            "message": str(exc)[:1000]}}
            LOG.error("Session request rejected: %s", exc)
        try:
            connection.settimeout(3)
            connection.sendall(json.dumps(result).encode() + b"\n")
        except OSError:
            LOG.info("Session caller exited during transition")
        finally:
            connection.close()
            slots.release()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET))
        # Discovery/open is public local IPC; kernel peer UID authorizes each
        # fixed action. Neither command can install, select URLs, or execute input.
        SOCKET.chmod(0o666)
        listener.listen(4)
        listener.settimeout(1)
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        try:
            while not stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                if slots.acquire(blocking=False):
                    threading.Thread(target=handle, args=(connection,), daemon=True).start()
                else:
                    connection.close()
        finally:
            SOCKET.unlink(missing_ok=True)


def serve(config):
    SOCKET.parent.mkdir(mode=0o755, exist_ok=True)
    trusted_path(SOCKET.parent, directory=True)
    singleton = Journal(SOCKET.parent / "broker")
    singleton.directory.mkdir(mode=0o700, exist_ok=True)
    trusted_path(singleton.directory, directory=True)
    with singleton.operation():
        _serve(config)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve(Config.load("/data/cloudplay/update/config.json"))
