"""Nonblocking native updater presentation; never accepts URLs or shell commands."""
from __future__ import annotations

from pathlib import Path
import json
import os
import queue
import re
import stat
import sys
import threading
import time

ENABLED = Path("/etc/cloudplay/ota-enabled")
COMMANDS = frozenset({"status", "check", "install", "cancel", "restart", "dismiss",
                      "open", "open-beta", "close", "enable_beta", "disable_beta"})
BUSY = frozenset({"checking", "downloading", "verifying", "staging", "installing",
                  "invalidating", "formatting", "copying", "publishing",
                  "staging_boot", "staging_root", "verifying_slot", "promoting",
                  "activating", "tryboot_running", "restarting", "finishing"})

# Closed vocabulary: daemon text and internal operation names never become UI copy.
OPERATIONS = {
    "download": (("downloading",), "Downloading your update", "downloaded"),
    "save_download": (("downloading",), "Saving your download", None),
    "authenticate": (("verifying",), "Checking that this update is safe to install", None),
    "check_package": (("verifying",), "Checking your download", "checked"),
    "unpack": (("verifying",), "Unpacking update files", "unpacked"),
    "save_archive": (("verifying",), "Saving update files", None),
    "archive_layout": (("verifying",), "Checking update contents", "checked"),
    "extract": (("verifying",), "Preparing update files", "prepared"),
    "file_attributes": (("verifying",), "Setting up update files", None),
    "check_prepared": (("verifying",), "Checking prepared files", "checked"),
    "check_source": (("invalidating",), "Rechecking prepared files before installation", "checked"),
    "prepare_storage": (("invalidating", "staging_boot", "staging_root"), "Getting your device ready", None),
    "preserve_profiles": (("invalidating",), "Keeping your saved sign-ins", None),
    "copy_boot": (("staging_boot",), "Installing startup files", "copied"),
    "save_boot": (("staging_boot", "publishing"), "Saving startup files", None),
    "copy_system": (("staging_root",), "Installing system files", "copied"),
    "configure_system": (("staging_root",), "Applying your device settings", None),
    "check_installed": (("verifying_slot",), "Checking installed files", "checked"),
    "save_system": (("verifying_slot",), "Saving system files", None),
    "check_final": (("publishing",), "Performing the final installation check", "checked"),
    "release_storage": (("publishing",), "Finishing up", None),
    "cleanup": (("finishing",), "Removing temporary update files", None),
    "check_restart": (("restarting",), "Checking files before restart", "checked"),
    "save_restart": (("restarting",), "Getting ready to restart", None),
}
STEPS = ("Download", "Prepare", "Install", "Check", "Restart")


def request_text(command):
    return {
        "check": "Checking for updates...",
        "install": "Starting your update...",
        "cancel": "Cancelling your update...",
        "restart": "Preparing to restart...",
        "status": "Refreshing update status...",
        "open": "Opening System Updates...",
        "open-beta": "Opening Beta Releases...",
        "close": "Returning to the Main Menu...",
        "enable_beta": "Turning on beta releases...",
        "disable_beta": "Turning off beta releases...",
        "dismiss": "",
    }[command]


def error_detail(error):
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        code = "UPDATE_ERROR"
    guidance = {
        "NETWORK": "Check your internet connection and try again.",
        "HTTP": "The update server is unavailable. Please try again later.",
        "BACKOFF": "Please wait a little before checking again.",
        "RATE_LIMIT": "Please wait a moment and try again.",
        "SPACE": "There isn't enough free space for this update.",
        "SIGNATURE": "This update could not be verified, so it won't be installed.",
        "RELEASE_CHANGED": "This update has changed. Check for updates again.",
        "BUSY": "Finish the current update before trying again.",
        "CANCEL_TOO_LATE": "Installation has started and can no longer be cancelled. Keep power connected.",
        "CANCELLED": "You can try the update again later.",
        "STRIKE_LIMIT": "Updates are paused after repeated failures. Get help before trying again.",
        "HARDWARE_GATE": "Updates aren't enabled on this device.",
        "ISOLATION_GATE": "This device isn't ready to install updates.",
        "UNINITIALIZED": "Update setup needs to be completed on this device.",
    }.get(code, "If this problem continues, use the reference below when asking for help.")
    return f"{guidance}\nReference: {code}"


def request_error(command, error):
    heading = {
        "check": "Unable to check for updates.",
        "install": "Unable to start the update.",
        "cancel": "Unable to cancel the update.",
        "restart": "Unable to restart to finish the update.",
        "open": "Unable to open System Updates.",
        "open-beta": "Unable to open Beta Releases.",
        "close": "Unable to return to the Main Menu.",
        "enable_beta": "Unable to change your beta preference.",
        "disable_beta": "Unable to change your beta preference.",
    }.get(command, "Update status is unavailable. Please try again.")
    return heading + "\n" + error_detail(error)


def operation(status):
    value = status.get("operation")
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return None
    spec = OPERATIONS.get(value["name"])
    return value if spec and status.get("phase") in spec[0] else None


def step_index(status):
    phase = status.get("phase")
    if phase == "downloading":
        return 0
    if phase == "verifying" or (operation(status) or {}).get("name") == "check_source":
        return 1
    if phase in ("invalidating", "staging_boot", "staging_root", "staging", "installing"):
        return 2
    if phase in ("verifying_slot", "publishing", "finishing"):
        return 3
    if phase in ("ready_to_restart", "restarting", "tryboot_running", "promoting", "promoted"):
        return 4
    return None


def journey(status):
    index = step_index(status)
    if index is None:
        return ()
    complete = status.get("phase") == "promoted"
    return tuple(
        (name, "done" if position < index or complete else
         "active" if position == index else "upcoming")
        for position, name in enumerate(STEPS))


def version_text(status):
    current = str(status.get("current_version") or "")[:128]
    target = str(status.get("candidate_version") or status.get("available_version") or "")[:128]
    if target and current != target:
        if status.get("phase") == "available":
            return f"Cloudplay OS {target} is available"
        return f"Updating Cloudplay OS to {target}"
    return "Cloudplay OS"


def request(command):
    parent = str(Path(__file__).resolve().parents[1])
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if command in ("open", "open-beta", "close"):
        from updater.maintenance import request as transition
        transition(command)
        return {"phase": "unknown"}
    if os.getuid() != 450:
        if command != "status":
            raise ValueError("Use the trusted update session for update controls")
        return public_status()
    from updater.client import request as send
    return send(command)


def initial_page(path=Path("/run/cloudplay-maintenance/landing-page")):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "updates"
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or info.st_size > 16):
        raise ValueError("Unsafe settings navigation state")
    page = path.read_text()
    if page not in ("updates", "beta"):
        raise ValueError("Invalid settings navigation state")
    return page


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
                install_enabled=False, can_cancel=False, can_restart=False,
                channel_change_enabled=False)


def progress_counts(status):
    sample = operation(status)
    if "operation" in status and status["operation"] is not None:
        if sample is None or OPERATIONS[sample["name"]][2] is None:
            return None
        progress = sample
    else:
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
        sample = operation(status)
        elapsed = sample.get("elapsed") if sample else None
        if type(elapsed) is int and 0 <= elapsed <= 31 * 86400:
            return f"Elapsed: {elapsed // 60}:{elapsed % 60:02d}"
        return "Waiting for progress..."
    received, total = counts
    return f"{100 * received // total}%"


def progress_detail(status):
    if status.get("cancellation_requested"):
        return "Cancelling your update. Waiting for the current task to stop safely."
    sample = operation(status)
    if sample:
        text = OPERATIONS[sample["name"]][1]
        if (progress_counts(status) is not None
                and type(sample.get("quiet_seconds")) is int and sample["quiet_seconds"] >= 15):
            text += ". No new progress reported yet."
        return text
    return {
        "downloading": "Connecting to the update service",
        "verifying": "Checking and preparing the downloaded files",
        "invalidating": "Getting your device ready",
        "formatting": "Getting your device ready",
        "staging": "Installing your update",
        "installing": "Installing your update",
        "copying": "Installing your update",
        "activating": "Preparing to restart",
        "staging_boot": "Preparing to install startup files",
        "staging_root": "Preparing to install system files",
        "verifying_slot": "Checking installed files",
        "publishing": "Saving and checking the installation",
        "finishing": "Finishing up before restart",
        "restarting": "Checking files and saving settings before restart",
        "tryboot_running": "Making sure Cloudplay is ready to use",
        "promoting": "Finishing your update",
    }.get(status.get("phase"), "Waiting for progress...")


def summary(status, *, include_progress=True):
    phase = status.get("phase", "unknown")
    last_check = status.get("last_successful_check")
    checked = type(last_check) in (int, float) and 0 <= last_check <= time.time()
    error = status.get("error")
    text = {
        "idle": "Cloudplay OS is up to date." if checked and not error else "Check for updates",
        "available": "An update is available.",
        "checking": "Checking for updates...",
        "downloading": "Downloading the update...",
        "verifying": "Checking your update...",
        "staging": "Installing your update. Keep power connected.",
        "installing": "Installing your update. Keep power connected.",
        "invalidating": "Preparing your device for installation. Keep power connected.",
        "staging_boot": "Installing startup files. Keep power connected.",
        "staging_root": "Installing the updated system. Do not remove power.",
        "verifying_slot": "Checking the installed files...",
        "publishing": "Finishing installation. Do not remove power.",
        "promoting": "Confirming the updated system...",
        "finishing": "Finishing installation. Keep power connected.",
        "ready_to_restart": "Select Finish Update to complete the update. Cloudplay will check the files and restart.",
        "restarting": "Rechecking the installed files before restart. Keep the power connected.",
        "tryboot_running": "Checking the updated system...",
        "promoted": "Update complete. Cloudplay is ready to play.",
        "rolled_back": "The update couldn't start. You're back on your previous version.",
        "failed": "The update could not be completed.",
        "disabled": "Updates aren't available on this installation.",
        "uninitialized": "This device needs update setup. Get help to complete it.",
        "recovery_required": "This device needs help to finish the update. It won't restart automatically.",
        "unknown": "Loading update status...",
    }.get(phase, "Update status is unavailable. Please try again.")
    if (phase in ("idle", "checking", "failed") and isinstance(error, dict)
            and error.get("command") == "check"):
        text = "Unable to check for updates."
    elif phase == "failed" and isinstance(error, dict) and error.get("code") == "CANCELLED":
        text = "Update cancelled."
    lines = [progress_detail(status) if phase in BUSY and phase != "checking" else text]
    if include_progress and progress_counts(status) is not None:
        lines.append(progress_text(status))
    if error:
        lines.append(error_detail(error))
    notes = status.get("notes")
    if isinstance(notes, str) and notes.strip() and phase == "available":
        lines.append("What's new:\n" + " ".join(notes.split())[:400])
    if status.get("gate") or status.get("mutation_enabled") is False:
        lines.append("Updates aren't enabled on this device.")
    return "\n".join(lines)


def badge(status):
    phase = status.get("phase")
    if phase == "available":
        return "Updates - new version available"
    if phase == "ready_to_restart":
        return "Updates - finish update"
    if phase in ("failed", "rolled_back", "recovery_required") and not status.get("notice_dismissed"):
        return "Updates - attention needed"
    if phase in BUSY:
        index = step_index(status)
        return "Updates - " + (STEPS[index].lower() if index is not None else "checking")
    return "Updates"


def actions(status):
    phase = status.get("phase", "unknown")
    result = []
    if status.get("requires_trusted_session"):
        return [("Try Again", "open")]
    if phase not in BUSY and phase != "ready_to_restart":
        result.append(("Check for Updates", "check"))
    if phase == "ready_to_restart":
        result.append(("Refresh", "status"))
    if phase == "available" and status.get("install_enabled") is True:
        result.append(("Install Update", "install"))
    if phase == "ready_to_restart" and status.get("can_restart") is True:
        result.append(("Finish Update", "restart"))
    if status.get("can_cancel") is True:
        result.append(("Cancel Update", "cancel"))
    if phase in ("failed", "rolled_back") and not status.get("notice_dismissed"):
        result.append(("Dismiss", "dismiss"))
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
        self.active_command = None
        self.queued_action = None
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
                print("Cloudplay update client: " + str(exc)[:400], file=sys.stderr)
                result = (None, request_error(command, {"code": getattr(exc, "code", None)}))
            self.results.put(result)

    def submit(self, command):
        if command not in COMMANDS:
            raise ValueError("Unsupported native update action")
        if self.closed.is_set():
            return False
        if self.pending:
            if self.active_command == "status" and command != "status" and self.queued_action is None:
                self.queued_action = command
                return True
            return False
        if command == "dismiss":
            self.dismissed = self.notice_key(self.status)
            self.status = dict(self.status, notice_dismissed=True)
            return True
        self.pending = True
        self.active_command = command
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
            self.active_command = None
            self.next_poll = self.clock() + 3
            if value is not None and self.dismissed == self.notice_key(value):
                value["notice_dismissed"] = True
            # A reply must clear temporary action text even when status is unchanged.
            changed = True
            self.error = error
            if value is not None:
                self.status = value
        if not self.pending and self.queued_action is not None:
            command, self.queued_action = self.queued_action, None
            self.submit(command)
        if not self.pending and self.clock() >= self.next_poll:
            self.submit("status")
        return changed

    def close(self):
        self.closed.set()
