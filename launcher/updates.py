"""Nonblocking native updater presentation; never accepts URLs or shell commands."""
from __future__ import annotations

from pathlib import Path
import json
import os
import queue
import stat
import sys
import threading
import time

ENABLED = Path("/etc/cloudplay/ota-enabled")
COMMANDS = frozenset({"status", "check", "install", "cancel", "restart", "dismiss", "open", "close"})
BUSY = frozenset({"checking", "downloading", "verifying", "staging", "installing",
                  "invalidating", "formatting", "copying", "publishing",
                  "staging_boot", "staging_root", "verifying_slot", "promoting",
                  "activating", "tryboot_running", "restarting"})


def request(command):
    parent = str(Path(__file__).resolve().parents[1])
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if command in ("open", "close"):
        from updater.maintenance import request as transition
        transition(command)
        return {"phase": "unknown"}
    if os.getuid() != 450:
        if command != "status":
            raise ValueError("Use the trusted update session for update controls")
        return public_status()
    from updater.client import request as send
    return send(command)


def public_status(path=Path("/run/cloudplay-updater/status.json")):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_size > 65536 or not 0 <= time.time() - info.st_mtime <= 30):
        raise ValueError("Update status is unsafe or stale")
    with path.open("rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("Update status exceeds its size bound")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Invalid public update status")
    return dict(value, requires_trusted_session=True,
                install_enabled=False, can_cancel=False, can_restart=False)


def progress_counts(status):
    if status.get("phase") not in ("downloading", "staging_boot", "staging_root"):
        return None
    progress = status.get("progress")
    if isinstance(progress, dict):
        received, total = progress.get("received"), progress.get("total")
        if (type(received) is int and type(total) is int
                and 0 <= received <= total <= 2**63 - 1 and total > 0):
            return received, total
    return None


def progress_fraction(status):
    counts = progress_counts(status)
    return counts[0] / counts[1] if counts is not None else None


def progress_text(status):
    counts = progress_counts(status)
    if counts is None:
        return "Working..."
    received, total = counts
    action = "downloaded" if status["phase"] == "downloading" else "copied"
    return (f"{100 * received // total}% {action}"
            f" ({received / 1024**2:.1f} / {total / 1024**2:.1f} MiB)")


def summary(status, *, include_progress=True):
    phase = status.get("phase", "unknown")
    version = status.get("current_version", "")
    available = status.get("available_version")
    text = {
        "idle": "No update is waiting.",
        "available": "An update is available.",
        "checking": "Checking for updates...",
        "downloading": "Downloading the update...",
        "verifying": "Verifying the signed update...",
        "staging": "Installing to the inactive system slot. Do not remove power.",
        "installing": "Installing to the inactive system slot. Do not remove power.",
        "invalidating": "Preparing the inactive system slot. Keep the power connected.",
        "staging_boot": "Installing boot files. Do not remove power.",
        "staging_root": "Installing the updated system. Do not remove power.",
        "verifying_slot": "Checking the installed files...",
        "publishing": "Finishing installation. Do not remove power.",
        "promoting": "Confirming the updated system...",
        "ready_to_restart": "The update is ready. Restart when you have finished playing.",
        "restarting": "Rechecking the installed files before restart. Keep the power connected.",
        "tryboot_running": "Checking the updated system...",
        "promoted": "The update is installed and the system checks passed.",
        "rolled_back": "The update did not start correctly. Your previous system was restored.",
        "failed": "The update could not be completed.",
        "disabled": "OTA installation is not enabled on this image.",
        "uninitialized": "The update service needs A/B image initialization.",
        "recovery_required": "The update needs local recovery. Automatic restart is stopped.",
        "unknown": "Waiting for the update service...",
    }.get(phase, "Update status: " + str(phase))
    lines = [text]
    if version:
        lines.append("Installed: " + str(version)[:128])
    if available:
        lines.append("Available: " + str(available)[:128])
    if include_progress and progress_counts(status) is not None:
        lines.append(progress_text(status))
    error = status.get("error")
    if error:
        if isinstance(error, dict):
            error = str(error.get("code", "ERROR")) + ": " + str(error.get("message", ""))
        lines.append(str(error)[:400])
    notes = status.get("notes")
    if isinstance(notes, str) and notes.strip() and phase == "available":
        lines.append(" ".join(notes.split())[:400])
    if status.get("gate") or status.get("mutation_enabled") is False:
        lines.append("Experimental preview: installation is locked until the safety gates pass.")
    if status.get("requires_trusted_session"):
        lines.append("Open Update Controls to enter a separate, browser-free system session.")
    return "\n".join(lines)


def badge(status):
    phase = status.get("phase")
    if phase == "available":
        return "Updates - new version available"
    if phase == "ready_to_restart":
        return "Updates - restart ready"
    if phase in ("failed", "rolled_back", "recovery_required") and not status.get("notice_dismissed"):
        return "Updates - attention needed"
    if phase in BUSY:
        return "Updates - " + phase.replace("_", " ")
    return "Updates"


def actions(status):
    phase = status.get("phase", "unknown")
    result = []
    if status.get("requires_trusted_session"):
        return [("Open Update Controls", "open")]
    if phase not in BUSY:
        result.append(("Check for Updates", "check"))
    if phase == "available" and status.get("install_enabled") is True:
        result.append(("Install Update", "install"))
    if phase == "ready_to_restart" and status.get("can_restart") is True:
        result.append(("Restart to Update", "restart"))
    if status.get("can_cancel") is True:
        result.append(("Cancel Download", "cancel"))
    if phase in ("failed", "rolled_back") and not status.get("notice_dismissed"):
        result.append(("Dismiss Notice", "dismiss"))
    return result


class Updates:
    """One bounded worker; Gtk only polls a local result queue."""

    def __init__(self, send=request, clock=time.monotonic):
        self.send, self.clock = send, clock
        self.status = {"phase": "unknown"}
        self.error = ""
        self.commands = queue.Queue(maxsize=2)
        self.results = queue.Queue(maxsize=4)
        self.closed = threading.Event()
        self.pending = False
        self.next_poll = 0
        self.dismissed = None
        self.worker = threading.Thread(target=self._work, daemon=True, name="cloudplay-update-client")
        self.worker.start()

    def _work(self):
        while not self.closed.is_set():
            try:
                command = self.commands.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                value = self.send(command)
                if not isinstance(value, dict):
                    raise ValueError("Invalid update-service response")
                result = (value, "")
            except (OSError, RuntimeError, ValueError) as exc:
                result = (None, str(exc)[:400])
                print("Cloudplay update client: " + result[1], file=sys.stderr)
            self.results.put(result)

    def submit(self, command):
        if command not in COMMANDS:
            raise ValueError("Unsupported native update action")
        if self.pending or self.closed.is_set():
            return False
        if command == "dismiss":
            self.dismissed = self.notice_key(self.status)
            self.status = dict(self.status, notice_dismissed=True)
            return True
        self.pending = True
        self.commands.put_nowait(command)
        return True

    @staticmethod
    def notice_key(status):
        return (status.get("phase"), str(status.get("error")), str(status.get("notice")))

    def poll(self):
        changed = False
        while True:
            try:
                value, error = self.results.get_nowait()
            except queue.Empty:
                break
            self.pending = False
            self.next_poll = self.clock() + 3
            if value is not None and self.dismissed == self.notice_key(value):
                value["notice_dismissed"] = True
            changed = changed or value != self.status or error != self.error
            self.error = error
            if value is not None:
                self.status = value
        if not self.pending and self.clock() >= self.next_poll:
            self.submit("status")
        return changed

    def close(self):
        self.closed.set()
