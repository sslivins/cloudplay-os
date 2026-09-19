"""Root AF_UNIX daemon and systemd-compatible reconciliation/health helpers."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import random
import signal
import socket
import stat
import struct
import threading
import time

from .client import MAX_REQUEST, MAX_RESPONSE, Client, receive_line
from .runtime import COMMANDS, ERRORS, Runtime
from .state import Config, Journal, UpdateError, atomic_write, fail, trusted_path
from .artifacts import canonical_json

LOG = logging.getLogger("cloudplay.updater")


def parse_request(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("IPC", "duplicate request field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("IPC", "invalid JSON request") from exc
    if (not isinstance(value, dict) or set(value) != {"command"}
            or not isinstance(value["command"], str) or value["command"] not in COMMANDS):
        fail("IPC", "request must contain exactly one fixed command")
    return value["command"]


def peer_uid(connection):
    if not hasattr(socket, "SO_PEERCRED"):
        fail("IPC_AUTH", "Linux SO_PEERCRED unavailable")
    _, uid, _ = struct.unpack("3i", connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid


class Service:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self._worker_lock = threading.Lock()
        self._clients = threading.BoundedSemaphore(4)
        self._stopping = threading.Event()
        self._worker = None
        self._last_mutation = {}
        self._rate_lock = threading.Lock()
        self._next_auto = time.time() + random.uniform(30, 300)
        self._command = None

    def status(self):
        status = self.runtime.status()
        if self._worker_lock.locked():
            status.update(can_restart=False, install_enabled=False)
            if status["phase"] == "ready_to_restart" and self._command == "install":
                status.update(phase="finishing", progress=None,
                              operation=dict(name="cleanup"))
        return status

    def _work(self, command, *, automatic=False):
        try:
            if command == "check":
                self.runtime.check(force=not automatic)
            else:
                getattr(self.runtime, command)()
        except ERRORS as exc:
            LOG.exception("Updater %s failed", command)
            try:
                with self.runtime.journal.operation(timeout=5):
                    self.runtime.journal.update(error=dict(
                        code=getattr(exc, "code", "IO"), message=str(exc)[:2048]))
            except ERRORS:
                LOG.exception("Cannot persist updater failure")
        except Exception as exc:
            # Boundary containment is visible in both journal and service logs.
            LOG.exception("Unexpected updater failure")
            try:
                with self.runtime.journal.operation(timeout=5):
                    self.runtime.journal.update(error=dict(
                        code="INTERNAL", message=f"{type(exc).__name__}: {exc}"[:2048]))
            except ERRORS:
                LOG.exception("Cannot persist internal updater failure")
        finally:
            self._worker_lock.release()

    def start(self, command, *, automatic=False):
        if not self._worker_lock.acquire(blocking=False):
            fail("BUSY", "another updater operation is running")
        self._command = command
        self._worker = threading.Thread(
            target=self._work, args=(command,), kwargs={"automatic": automatic},
            name="cloudplay-update", daemon=False)
        try:
            self._worker.start()
        except RuntimeError as exc:
            self._worker = None
            self._worker_lock.release()
            raise UpdateError("WORKER", "cannot start updater operation thread") from exc

    def dispatch(self, command, uid):
        if uid not in (0, self.runtime.config.launcher_uid):
            fail("IPC_AUTH", "peer is not the dedicated launcher identity")
        if command == "status":
            return self.status()
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_mutation.get(uid, -10) < 1:
                fail("RATE_LIMIT", "wait before issuing another command")
            self._last_mutation[uid] = now
        if command in ("install", "restart"):
            self.runtime.config.mutation_gate()
        if command == "cancel":
            return self.runtime.cancel()
        self.start(command)
        return self.status()

    def _connection(self, connection):
        try:
            connection.settimeout(3)
            try:
                uid = peer_uid(connection)
                if uid not in (0, self.runtime.config.launcher_uid):
                    fail("IPC_AUTH", "unauthorized Unix peer")
                command = parse_request(receive_line(connection, MAX_REQUEST, timeout=3))
                response = dict(ok=True, status=self.dispatch(command, uid))
            except ERRORS as exc:
                response = dict(ok=False, error=dict(
                    code=getattr(exc, "code", "IPC"), message=str(exc)[:2048]))
            raw = canonical_json(response) + b"\n"
            if len(raw) > MAX_RESPONSE:
                raw = canonical_json(dict(ok=False, error=dict(
                    code="IPC_LIMIT", message="status exceeds response limit"))) + b"\n"
            connection.sendall(raw)
        except OSError:
            LOG.exception("Updater client disconnected or exceeded deadline")
        finally:
            connection.close()
            self._clients.release()

    def _tick(self):
        if self.runtime.journal.path.exists() and not self._worker_lock.locked():
            state = self.runtime.journal.load()
            if state["phase"] in ("tryboot_running", "promoting"):
                try:
                    self.runtime.health(deadline_only=True)
                except ERRORS:
                    LOG.exception("Candidate deadline handler failed; external watchdog remains required")
            elif (not state["pending"] and time.time() >= self._next_auto
                  and time.time() >= self.runtime.discovery.next_check_at):
                self._next_auto = time.time() + 60 + random.uniform(0, 300)
                self.start("check", automatic=True)
        if self.runtime.journal.path.exists():
            path = Path(self.runtime.config.socket_path).parent / "status.json"
            atomic_write(path, canonical_json(self.status()), 0o644)

    def stop(self, *_):
        self._stopping.set()

    def serve(self):
        if os.name != "posix" or os.geteuid() != 0 or not hasattr(socket, "SO_PEERCRED"):
            fail("ROOT", "daemon requires Linux root and SO_PEERCRED")
        config = self.runtime.config
        directory = Path(config.socket_path).parent
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        trusted_path(directory, directory=True)
        os.chown(directory, 0, config.socket_gid)
        os.chmod(directory, 0o755)
        provider_lock = directory / "provider.lock"
        if not provider_lock.exists():
            fd = os.open(provider_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.close(fd)
        trusted_path(provider_lock)
        if not stat.S_ISREG(provider_lock.lstat().st_mode):
            fail("IPC", "provider interlock must be a regular root-owned file")
        os.chmod(provider_lock, 0o644)
        if Path(config.state_dir).exists():
            trusted_path(Path(config.state_dir), directory=True)
        path = Path(config.socket_path)
        # Separate lifetime lock prevents a second daemon unlinking our socket.
        daemon_lock = Journal(directory / "daemon")
        daemon_lock.directory.mkdir(mode=0o700, exist_ok=True)
        trusted_path(daemon_lock.directory, directory=True)
        with daemon_lock.operation():
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                    fail("IPC", "unsafe existing socket path")
                path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                os.chown(path, 0, config.socket_gid)
                os.chmod(path, 0o660)
                listener.listen(4)
                listener.settimeout(1)
                signal.signal(signal.SIGTERM, self.stop)
                signal.signal(signal.SIGINT, self.stop)
                try:
                    while not self._stopping.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            connection = None
                        if connection is not None:
                            if self._clients.acquire(blocking=False):
                                threading.Thread(target=self._connection, args=(connection,),
                                                 daemon=True).start()
                            else:
                                connection.close()
                        try:
                            self._tick()
                        except ERRORS:
                            LOG.exception("Updater timer/status publication failed")
                finally:
                    path.unlink(missing_ok=True)
        # Do not kill a writer mid-operation. systemd must allow its bounded
        # operation to finish; KillMode/process watchdog policy is documented.
        if self._worker is not None:
            self._worker.join()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/data/cloudplay/update/config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "initialize", "reconcile", "health", "deadline",
                 "shutdown", "retry", "rollback", "early-guard"):
        sub.add_parser(name)
    client = sub.add_parser("client", help="fixed unprivileged socket command")
    client.add_argument("request", choices=sorted(COMMANDS))
    client.add_argument("--socket", default="/run/cloudplay-updater/control.sock")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "client":
            result = Client(args.socket).request(args.request)
        elif args.command == "early-guard":
            if os.name != "posix" or os.geteuid() != 0:
                fail("ROOT", "early boot guard requires Linux root")
            from .boot import EarlyGuard
            result = {"early_guard": EarlyGuard().run()}
        else:
            config = Config.load(args.config)
            runtime = Runtime(config)
            if args.command == "serve":
                Service(runtime).serve()
                return 0
            if args.command == "deadline":
                result = runtime.health(deadline_only=True)
            else:
                result = getattr(runtime, args.command)()
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok", True) else 1
    except ERRORS as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
