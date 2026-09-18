"""Durable updater journal and root-owned configuration (no device writes)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import stat
import threading
import uuid

from .artifacts import SemVer, canonical_json

PHASES = frozenset({
    "idle", "checking", "available", "downloading", "verifying", "invalidating",
    "staging_boot", "staging_root", "verifying_slot", "publishing",
    "ready_to_restart", "tryboot_running", "promoting", "promoted",
    "rolled_back", "failed", "recovery_required",
})


class UpdateError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def fail(code, message):
    raise UpdateError(code, message)


def read_json(path: Path, limit=1024 * 1024):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("STATE", "duplicate JSON field")
            result[key] = value
        return result
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            fail("STATE", f"not a bounded regular file: {path}")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            fail("STATE", "JSON exceeds size bound")
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: fail("STATE", "nonfinite JSON"))
        if not isinstance(value, dict):
            fail("STATE", "expected JSON object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise UpdateError("STATE", f"cannot read {path}: {exc}") from exc


def trusted_path(path: Path, *, directory=False):
    """Reject symlink ancestry and anything writable by a non-root identity."""
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail("CONFIG", f"symlink in trusted path: {item}")
        if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o022):
            fail("CONFIG", f"trusted path is not root controlled: {item}")
    if directory and not path.is_dir():
        fail("CONFIG", f"directory required: {path}")


def sync_directory(path: Path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, data: bytes, mode=0o600):
    if path.is_symlink():
        fail("STATE", "refusing symlink destination")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Config:
    schema: int = 1
    experimental_hardware_validation: bool = False
    launcher_isolation_verified: bool = False
    isolation_evidence: str = ""
    hardware_evidence: str = ""
    launcher_uid: int = 450
    browser_uid: int = 1000
    socket_gid: int = 450
    socket_path: str = "/run/cloudplay-updater/control.sock"
    state_dir: str = "/data/cloudplay/update"
    keys_dir: str = "/usr/share/cloudplay/update-keys"
    release_file: str = "/usr/share/cloudplay/release.json"
    profile_root: str = "/data/cloudplay/profiles"
    profile_mount: str = "/home/cloudplay/.config/cloudplay"
    channel: str = "beta"
    platform: str = "cm5"
    repo: str = "sslivins/cloudplay-os"
    data_schema: int = 1
    minimum_key_epoch: int = 1
    minimum_eeprom: str = ""
    boot_order: str = ""
    stabilization_seconds: int = 120
    deadline_seconds: int = 600
    strike_limit: int = 3

    @classmethod
    def load(cls, path):
        path = Path(path)
        trusted_path(path)
        value = read_json(path, 16384)
        if set(value) - {f.name for f in fields(cls)}:
            fail("CONFIG", "unknown configuration fields")
        result = cls(**value)
        result.validate()
        return result

    def validate(self):
        if type(self.schema) is not int or self.schema != 1:
            fail("CONFIG", "unsupported schema")
        for name in ("experimental_hardware_validation", "launcher_isolation_verified"):
            if type(getattr(self, name)) is not bool:
                fail("CONFIG", f"{name} must be a boolean")
        for name in ("launcher_uid", "browser_uid", "socket_gid", "data_schema",
                     "minimum_key_epoch", "stabilization_seconds", "deadline_seconds",
                     "strike_limit"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                fail("CONFIG", f"invalid {name}")
        if not 30 <= self.stabilization_seconds < self.deadline_seconds <= 1800:
            fail("CONFIG", "invalid health time bounds")
        if self.channel not in ("stable", "beta") or self.platform not in ("pi5", "cm5"):
            fail("CONFIG", "invalid platform/channel")
        for name in ("isolation_evidence", "hardware_evidence", "minimum_eeprom", "boot_order", "repo"):
            if not isinstance(getattr(self, name), str) or len(getattr(self, name)) > 2048:
                fail("CONFIG", f"invalid {name}")
        for name in ("socket_path", "state_dir", "keys_dir", "release_file",
                     "profile_root", "profile_mount"):
            value = getattr(self, name)
            if (not isinstance(value, str) or not Path(value).is_absolute()
                    or any(c.isspace() for c in value) or "\x00" in value):
                fail("CONFIG", f"{name} must be absolute")

    def mutation_gate(self):
        if not self.experimental_hardware_validation or not self.hardware_evidence:
            fail("HARDWARE_GATE", "OTA hardware acceptance incomplete; explicit root experimental approval required")
        if (not self.launcher_isolation_verified or not self.isolation_evidence
                or self.launcher_uid == self.browser_uid):
            fail("ISOLATION_GATE", "separate browser identity and independently verified launcher isolation required")
        if os.name != "posix" or os.geteuid() != 0:
            fail("ROOT", "physical OTA operations require Linux root")


class Journal:
    """One process operation lock plus serialized, fsync-backed JSON transactions."""
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "state.json"
        self._mutex = threading.RLock()

    @contextmanager
    def operation(self):
        if self.directory.is_symlink() or not self.directory.is_dir():
            fail("STATE", "provisioned persistent state directory is unavailable")
        path = self.directory / "operation.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise UpdateError("BUSY", "another updater operation holds the lock") from exc
            else:
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise UpdateError("BUSY", "another updater operation holds the lock") from exc
            yield
        finally:
            os.close(fd)

    def load(self):
        with self._mutex:
            value = read_json(self.path, 40 * 1024 * 1024)
            required = {"schema", "phase", "current_version", "highest_version",
                        "minimum_key_epoch", "last_good", "pending", "strikes", "error"}
            if (not required <= value.keys() or type(value["schema"]) is not int
                    or value["schema"] != 1):
                fail("STATE", "journal schema mismatch; operator recovery required")
            if not isinstance(value["phase"], str) or value["phase"] not in PHASES:
                fail("STATE", "invalid journal phase")
            SemVer.parse(value["current_version"])
            SemVer.parse(value["highest_version"])
            if (value["last_good"] not in ("A", "B")
                    or type(value["strikes"]) is not int or value["strikes"] < 0
                    or type(value["minimum_key_epoch"]) is not int
                    or value["minimum_key_epoch"] < 1):
                fail("STATE", "invalid journal identity/floors")
            if SemVer.parse(value["highest_version"]) < SemVer.parse(value["current_version"]):
                fail("STATE", "highest accepted version is below current version")
            pending = value["pending"]
            if pending is not None:
                if (not isinstance(pending, dict) or pending.get("slot") not in ("A", "B")
                        or pending["slot"] == value["last_good"]
                        or not isinstance(pending.get("metadata"), dict)
                        or not {"version", "manifest", "manifest_sha256", "key_epoch"} <= pending["metadata"].keys()
                        or not isinstance(pending["metadata"]["manifest"], dict)
                        or type(pending.get("attempted")) is not bool
                        or type(pending.get("rollback_attempted")) is not bool):
                    fail("STATE", "invalid durable candidate")
                SemVer.parse(pending["metadata"]["version"])
                if pending.get("boot_id") is not None:
                    if (not isinstance(pending["boot_id"], str)
                            or any(type(pending.get(k)) not in (int, float)
                                   for k in ("started", "deadline"))):
                        fail("STATE", "invalid candidate boot/deadline record")
            return value

    def initialize(self, version: str, slot: str, epoch: int, *, identity=None):
        SemVer.parse(version)
        if slot not in ("A", "B") or type(epoch) is not int or epoch < 1:
            fail("STATE", "invalid initialization")
        if self.path.exists():
            fail("STATE", "journal already exists")
        self.save(dict(schema=1, phase="idle", current_version=version,
                       highest_version=version, minimum_key_epoch=epoch,
                       last_good=slot, last_good_identity=identity,
                       pending=None, strikes=0, error=None))

    def save(self, value):
        with self._mutex:
            atomic_write(self.path, canonical_json(value))

    def update(self, **changes):
        with self._mutex:
            value = self.load()
            value.update(changes)
            self.save(value)
            return value
