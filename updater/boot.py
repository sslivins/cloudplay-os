"""Boot-local candidate deadline, independent of /data and the graphical session."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import time

from .artifacts import SemVer, canonical_json
from .state import (Config, Journal, UpdateError, atomic_write, confirmation_deadline,
                    fail, read_json, trusted_path)

BOOT_DIR = Path("/boot/firmware")
RUN_DIR = Path("/run/cloudplay-early")
APPROVAL_FIELDS = frozenset({
    "experimental_hardware_validation", "hardware_evidence",
    "launcher_isolation_verified", "isolation_evidence",
    "launcher_uid", "browser_uid",
})


def make_ticket(config, layout_record, previous):
    config.mutation_gate()
    approval = {name: getattr(config, name) for name in APPROVAL_FIELDS}
    for name in ("hardware_evidence", "isolation_evidence"):
        approval[name] = hashlib.sha256(approval[name].encode()).hexdigest()
    return dict(schema=1, armed=True, confirmed=False, attempted=False,
                deadline_seconds=config.deadline_seconds, approval=approval,
                layout=layout_record, previous=previous)


def validate_ticket(sentinel, *, run_dir=RUN_DIR):
    ticket = sentinel.get("guard")
    if ticket is None:
        return None
    required = {"schema", "armed", "confirmed", "attempted", "deadline_seconds",
                "approval", "layout", "previous"}
    if (not isinstance(ticket, dict) or set(ticket) != required
            or type(ticket["schema"]) is not int or ticket["schema"] != 1
            or any(type(ticket[key]) is not bool for key in ("armed", "confirmed", "attempted"))
            or type(ticket["deadline_seconds"]) is not int
            or not 31 <= ticket["deadline_seconds"] <= 1800
            or not isinstance(ticket["approval"], dict)
            or set(ticket["approval"]) != APPROVAL_FIELDS
            or not isinstance(ticket["previous"], dict)
            or not isinstance(ticket["layout"], dict)
            or set(ticket["previous"]) != {"slot", "version", "manifest_sha256", "config_sha256"}
            or ticket["previous"]["slot"] not in ("A", "B")
            or sentinel.get("slot") not in ("A", "B")
            or ticket["previous"]["slot"] == sentinel["slot"]
            or (ticket["armed"], ticket["confirmed"], ticket["attempted"]) not in (
                (True, False, False), (True, False, True), (False, True, False))):
        fail("BOOT_GUARD", "invalid boot-local candidate capability")
    SemVer.parse(sentinel.get("version"))
    SemVer.parse(ticket["previous"]["version"])
    for value in (sentinel.get("manifest_sha256"), ticket["previous"]["manifest_sha256"],
                  ticket["previous"]["config_sha256"]):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            fail("BOOT_GUARD", "invalid boot-local identity digest")
    # There are no caller-selected command/path capabilities in the ticket.
    config = Config(
        **ticket["approval"], deadline_seconds=ticket["deadline_seconds"],
        stabilization_seconds=30, state_dir=str(run_dir),
        socket_path=str(run_dir / "socket"), keys_dir=str(run_dir),
        release_file=str(run_dir / "release"), profile_root=str(run_dir / "profiles"),
        profile_mount=str(run_dir / "profile"))
    config.validate()
    config.mutation_gate()
    return ticket, config


@contextmanager
def guard_lock(directory=RUN_DIR):
    directory.mkdir(mode=0o700, exist_ok=True)
    trusted_path(directory, directory=True)
    with Journal(directory).operation():
        yield


def read_sentinel(boot_dir=BOOT_DIR):
    path = boot_dir / "slot-valid.json"
    trusted_path(path)
    return read_json(path, 4096)


def write_sentinel(platform, value, boot_dir=BOOT_DIR):
    platform.config.mutation_gate()
    atomic_write(boot_dir / "slot-valid.json", canonical_json(value), 0o644)
    platform.flush(boot_dir)
    if read_json(boot_dir / "slot-valid.json", 4096) != value:
        fail("BOOT_GUARD", "candidate capability readback failed")


class EarlyGuard:
    """Wait against boot monotonic time; service restarts never reset the limit."""
    def __init__(self, *, boot_dir=BOOT_DIR, run_dir=RUN_DIR, platform_factory=None,
                 clock=time.monotonic, sleep=time.sleep):
        if platform_factory is None:
            from .platform import LinuxPlatform
            platform_factory = LinuxPlatform
        self.boot_dir, self.run_dir = Path(boot_dir), Path(run_dir)
        self.platform_factory, self.clock, self.sleep = platform_factory, clock, sleep

    def run(self):
        initial = read_sentinel(self.boot_dir)
        parsed = validate_ticket(initial, run_dir=self.run_dir)
        if parsed is None:
            return "baseline"
        ticket, config = parsed
        if ticket["attempted"]:
            fail("ROLLBACK_LOOP", "boot-local rollback already attempted; local recovery required")
        if not ticket["armed"]:
            return "confirmed"
        platform = self.platform_factory(config)
        while True:
            current = read_sentinel(self.boot_dir)
            if any(current.get(key) != initial.get(key)
                   for key in ("slot", "version", "manifest_sha256")):
                fail("BOOT_GUARD", "running sentinel identity changed")
            parsed = validate_ticket(current, run_dir=self.run_dir)
            if parsed is None:
                fail("BOOT_GUARD", "candidate capability disappeared")
            ticket, _ = parsed
            if any(ticket[key] != initial["guard"][key] for key in ticket
                   if key not in ("armed", "confirmed", "attempted")):
                fail("BOOT_GUARD", "candidate capability policy changed while waiting")
            if ticket["attempted"]:
                fail("ROLLBACK_LOOP", "rollback was already attempted; refusing another reboot")
            if ticket["confirmed"]:
                return "confirmed"
            if not ticket["armed"]:
                fail("BOOT_GUARD", "candidate capability is neither armed nor confirmed")
            deadline = confirmation_deadline(ticket["deadline_seconds"])
            remaining = deadline - self.clock()
            if remaining > 0:
                self.sleep(min(5, remaining))
                continue
            try:
                with guard_lock(self.run_dir):
                    current = read_sentinel(self.boot_dir)
                    parsed = validate_ticket(current, run_dir=self.run_dir)
                    if parsed is None or any(current.get(key) != initial.get(key)
                                             for key in ("slot", "version", "manifest_sha256")):
                        fail("BOOT_GUARD", "candidate identity/capability changed during deadline handling")
                    ticket, _ = parsed
                    if any(ticket[key] != initial["guard"][key] for key in ticket
                           if key not in ("armed", "confirmed", "attempted")):
                        fail("BOOT_GUARD", "candidate capability policy changed")
                    if ticket["confirmed"]:
                        return "confirmed"
                    if ticket["attempted"]:
                        fail("ROLLBACK_LOOP", "rollback already attempted")
                    layout = platform.inspect_early(ticket["layout"])
                    if layout.active != current["slot"]:
                        fail("BOOT_GUARD", "capability does not describe the mounted slot")
                    platform.verify_good(layout, ticket["previous"])
                    ticket["attempted"] = True
                    write_sentinel(platform, current, self.boot_dir)
                    platform.quarantine_running()
                    platform.write_pointers(layout, ticket["previous"]["slot"],
                                            ticket["previous"]["slot"])
                    platform.reboot()
                    return "rollback_requested"
            except UpdateError as exc:
                if exc.code != "BUSY":
                    raise
                # A bounded promotion/rollback operation owns the same lock.
                if self.clock() >= deadline + 120:
                    fail("BOOT_GUARD_BUSY", "promotion/rollback lock exceeded its deadline; local recovery required")
                self.sleep(1)
