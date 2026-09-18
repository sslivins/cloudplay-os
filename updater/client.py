"""Unprivileged fixed-command client; never passes URLs, paths or shell strings."""
from __future__ import annotations

import json
import socket
import time

from .runtime import COMMANDS
from .state import UpdateError

MAX_REQUEST = 1024
MAX_RESPONSE = 65536
SOCKET_PATH = "/run/cloudplay-updater/control.sock"


def receive_line(connection, limit, *, timeout=5):
    data = bytearray()
    deadline = time.monotonic() + timeout
    while len(data) <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateError("IPC_TIMEOUT", "message deadline expired")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, limit + 1 - len(data)))
        if not chunk:
            raise UpdateError("IPC", "connection closed before complete response")
        data.extend(chunk)
        if b"\n" in chunk:
            if not data.endswith(b"\n") or data.count(b"\n") != 1:
                raise UpdateError("IPC", "only one JSON frame is permitted")
            if len(data) > limit:
                break
            return bytes(data[:-1])
    raise UpdateError("IPC_LIMIT", "message exceeds byte limit")


class Client:
    def __init__(self, socket_path=SOCKET_PATH, *, timeout=5):
        if not 0 < timeout <= 30:
            raise ValueError("timeout must be within (0,30]")
        self.socket_path, self.timeout = socket_path, timeout

    def request(self, command):
        if command not in COMMANDS:
            raise UpdateError("COMMAND", "unknown updater command")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(json.dumps({"command": command}).encode() + b"\n")
                result = json.loads(receive_line(connection, MAX_RESPONSE, timeout=self.timeout))
        except (OSError, ValueError) as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("IPC", f"updater unavailable: {exc}") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise UpdateError("IPC", "invalid updater response")
        return result

    def _command(self, name):
        result = self.request(name)
        if not result["ok"]:
            error = result.get("error", {})
            raise UpdateError(error.get("code", "IPC"), error.get("message", "updater rejected request"))
        return result["status"]

    def status(self):
        return self._command("status")

    def check(self):
        return self._command("check")

    def install(self):
        return self._command("install")

    def cancel(self):
        return self._command("cancel")

    def restart(self):
        return self._command("restart")


def request(command, *, socket_path=SOCKET_PATH, timeout=5):
    """Native launcher convenience: return status or raise a typed error."""
    return Client(socket_path, timeout=timeout)._command(command)
