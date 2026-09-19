"""Linux physical-slot operations. All mutation requires explicit root gates."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time

from .artifacts import (GENERATED_PATHS, _read, _set_attributes, canonical_json,
                        sha256_file, verify_attributes, verify_tree)
from .state import (Config, UpdateError, atomic_write, fail, read_json,
                    sync_directory, trusted_path)

MIB = 1024**2
BASIC = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"
GEOMETRY = ((4, 64), (68, 1024), (1092, 1024), (2116, 8192),
            (10308, 8192), (18500, None))
LABELS = ("boot-control", "boot-A", "boot-B", "root-A", "root-B", "data")
BOOT = {"A": 2, "B": 3}
ROOT = {"A": 4, "B": 5}
HEALTH_UNITS = ("cloudplay-network.service", "cloudplay-startup.service", "cloudplay-maintenance.service")


def run(args, *, timeout=30, allowed=(0,)):
    """Every command has a deadline; arguments are never interpreted by a shell."""
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("COMMAND", f"{args[0]} failed: {exc}") from exc
    if result.returncode not in allowed:
        fail("COMMAND", f"{args[0]} exited {result.returncode}: {result.stderr[:1024]}")
    if len(result.stdout) > 4 * MIB or len(result.stderr) > MIB:
        fail("COMMAND", "command output exceeded bound")
    return result


@dataclass(frozen=True)
class Partition:
    number: int
    node: str
    uuid: str
    size: int


@dataclass(frozen=True)
class Layout:
    disk: str
    active: str
    partitions: tuple[Partition, ...]

    def part(self, number):
        return self.partitions[number - 1]

    @property
    def target(self):
        return "B" if self.active == "A" else "A"


def validate_table(table: dict, *, disk: str, root_number: int, boot_number: int):
    if (table.get("label") != "gpt" or table.get("sectorsize") != 512
            or table.get("device") != disk or root_number not in (4, 5)):
        fail("LAYOUT", "unsupported GPT/root device")
    active = "A" if root_number == 4 else "B"
    if boot_number != BOOT[active]:
        fail("LAYOUT", "mounted root and boot are different slots")
    parts = table.get("partitions", [])
    if len(parts) != 6:
        fail("LAYOUT", "exactly six FAT-first partitions required")
    result = []
    for number, (part, (start, size)) in enumerate(zip(parts, GEOMETRY), 1):
        uuid = part.get("uuid", "")
        if (part.get("start") != start * MIB // 512
                or type(part.get("size")) is not int
                or (size is not None and part["size"] != size * MIB // 512)
                or (size is None and part["size"] < 1024 * MIB // 512)
                or part.get("name") != LABELS[number - 1]
                or part.get("type", "").lower() != (BASIC if number <= 3 else LINUX)
                or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid)
                or not isinstance(part.get("node"), str)):
            fail("LAYOUT", f"partition {number} identity/type/size mismatch")
        result.append(Partition(number, part["node"], uuid.lower(), part["size"] * 512))
    if len({p.uuid for p in result}) != 6 or len({p.node for p in result}) != 6:
        fail("LAYOUT", "duplicate partition identity")
    return Layout(disk, active, tuple(result))


def validate_layout_record(value: dict, table: dict):
    disk_id = table.get("id")
    if not isinstance(disk_id, str) or not disk_id:
        fail("LAYOUT", "GPT disk identity is missing")
    expected = dict(schema=1, disk_id=disk_id,
                    partitions={part["name"]: part["uuid"]
                                for part in table["partitions"]})
    if type(value.get("schema")) is not int or value != expected:
        fail("LAYOUT_RECORD", "persistent layout record does not match the mounted disk GPT")


def safe_child(root: Path, relative: str):
    """Do not traverse payload symlinks, including absolute target-root links."""
    parts = relative.split("/")
    if not relative or any(p in ("", ".", "..") for p in parts) or "\\" in relative:
        fail("PATH", "invalid target-relative path")
    current = root
    if root.is_symlink() or not root.is_dir():
        fail("PATH", "unsafe filesystem root")
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            fail("PATH", "generated path has a non-directory parent")
    target = current / parts[-1]
    if target.is_symlink():
        fail("PATH", "generated path is a symlink")
    return target


def copy_payload(source: Path, target: Path, metadata: dict, prefix: str,
                 withheld=frozenset(), *, progress=None):
    records = {name: record for name, record in metadata["manifest"].items()
               if name == prefix or name.startswith(prefix + "/")}
    total = sum(record["size"] for name, record in records.items()
                if record["type"] == "file" and name not in withheld)
    received = 0
    if progress:
        progress(received, total)
    links, directories = [], []
    for name, record in sorted(records.items(), key=lambda pair: (pair[0].count("/"), pair[0])):
        if name == prefix:
            directories.append((target, record))
            continue
        if name in withheld:
            continue
        relative = name[len(prefix) + 1:]
        destination = safe_child(target, relative)
        origin = source.joinpath(*name.split("/"))
        if record["type"] == "directory":
            destination.mkdir(mode=0o700)
            directories.append((destination, record))
        elif record["type"] == "symlink":
            links.append((destination, record))
        else:
            with origin.open("rb") as src, destination.open("xb") as out:
                while chunk := src.read(MIB):
                    out.write(chunk)
                    received += len(chunk)
                    if progress and received < total:
                        progress(received, total)
                out.flush()
                os.fsync(out.fileno())
            if prefix == "root":
                _set_attributes(destination, record)
    for destination, record in links:
        os.symlink(record["target"], destination)
        _set_attributes(destination, record)
    if prefix == "root":
        for destination, record in reversed(directories):
            _set_attributes(destination, record)
    if progress:
        progress(received, total)


def verify_slot(root: Path, boot: Path, metadata: dict, generated: dict,
                withheld=frozenset(), *, activity=None, operation="check_installed"):
    """Closed exemptions are exact expected bytes+metadata, never wildcard skips."""
    if not set(generated) <= GENERATED_PATHS:
        fail("GENERATED", "unknown generated-file exemption")
    expected = dict(metadata["manifest"])
    expected.update(generated)
    total = sum(record["size"] for name, record in expected.items()
                if name not in withheld and record["type"] == "file")
    checked = 0
    def report(count):
        nonlocal checked
        checked += count
        if activity:
            activity(operation, checked, total)
    if activity:
        activity(operation, 0, total)
    seen = set()
    for prefix, base in (("root", root), ("boot", boot)):
        pending = [(prefix, base)]
        while pending:
            name, path = pending.pop()
            info = path.lstat()
            record = expected.get(name)
            if record is None or name in withheld:
                fail("SLOT_VERIFY", f"unexpected member {name}")
            seen.add(name)
            kind = record["type"]
            if kind == "directory" and stat.S_ISDIR(info.st_mode):
                pending.extend((name + "/" + child.name, child) for child in path.iterdir())
            elif kind == "symlink" and stat.S_ISLNK(info.st_mode):
                if os.readlink(path) != record["target"]:
                    fail("SLOT_VERIFY", f"link mismatch: {name}")
            elif kind == "file" and stat.S_ISREG(info.st_mode):
                if (info.st_nlink != 1 or info.st_size != record["size"]
                        or sha256_file(path, progress=report) != record["sha256"]):
                    fail("SLOT_VERIFY", f"content mismatch: {name}")
            else:
                fail("SLOT_VERIFY", f"type mismatch: {name}")
            if prefix == "root":
                verify_attributes(path, record)
    if seen != set(expected) - set(withheld):
        fail("SLOT_VERIFY", "missing signed/generated content")


def file_record(data: bytes, *, mode=0o644, uid=0, gid=0):
    return dict(type="file", mode=mode, uid=uid, gid=gid,
                size=len(data), sha256=hashlib.sha256(data).hexdigest())


def render_cmdline(root: Path, metadata: dict, root_uuid: str):
    name = "root/usr/share/cloudplay/kernel-command-line.txt"
    path = safe_child(root, name.split("/", 1)[1])
    record = metadata["manifest"].get(name)
    if not record or record["type"] != "file" or sha256_file(path) != record["sha256"]:
        fail("BOOT_CONFIG", "signed kernel command-line template is missing or changed")
    try:
        template = _read(path, 4096).decode("ascii")
    except UnicodeError as exc:
        raise UpdateError("BOOT_CONFIG", "kernel command-line template must be ASCII") from exc
    if "\x00" in template:
        fail("BOOT_CONFIG", "invalid kernel command-line template")
    arguments = [arg for arg in template.split() if not arg.startswith(
        ("root=", "init=", "systemd.run=", "systemd.run_success_action=",
         "systemd.run_failure_action=")) and arg not in ("ro", "rw", "resize")]
    return (" ".join(arguments + [f"root=PARTUUID={root_uuid}", "rw"]) + "\n").encode()


def identity_files(persistent: Path, current: Path):
    """Copy only seeded identity that agrees with the currently running device."""
    machine = persistent / "machine-id"
    trusted_path(machine)
    raw = _read(machine, 128)
    if (not re.fullmatch(rb"[0-9a-f]{32}\n?", raw)
            or raw.strip() == b"0" * 32
            or raw.strip() != _read(current / "machine-id", 128).strip()):
        fail("IDENTITY", "persistent machine-id is invalid or differs from the active identity")
    files = {"root/etc/machine-id": (raw, 0o644)}
    for kind in ("rsa", "ecdsa", "ed25519"):
        private = persistent / "ssh" / f"ssh_host_{kind}_key"
        public = private.with_name(private.name + ".pub")
        if private.exists() != public.exists():
            fail("IDENTITY", "incomplete persistent SSH host-key pair")
        for path, mode in ((private, 0o600), (public, 0o644)):
            active = current / "ssh" / path.name
            if not path.exists():
                if path.is_symlink() or active.exists() or active.is_symlink():
                    fail("IDENTITY", "active SSH identity is missing from persistent storage")
                continue
            trusted_path(path)
            info = path.lstat()
            if os.name == "posix" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode):
                fail("IDENTITY", "persistent SSH host-key ownership or mode is invalid")
            data = _read(path, 16384)
            if data != _read(active, 16384):
                fail("IDENTITY", "persistent SSH key differs from active identity")
            files["root/etc/ssh/" + path.name] = (data, mode)
    return files


class LinuxPlatform:
    def __init__(self, config: Config, *, runner=run):
        self.config, self.run = config, runner
        self.work = Path(config.state_dir) / "mounts"

    def mutate(self, args, *, timeout=60):
        self.config.mutation_gate()
        return self.run(args, timeout=timeout)

    @staticmethod
    def boot_id():
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def unmounted(self, part):
        result = self.run(["findmnt", "--noheadings", "--source", part.node], allowed=(0, 1))
        if result.returncode == 0:
            fail("MOUNT", f"partition {part.number} is already mounted")
        for line in Path("/proc/swaps").read_text().splitlines()[1:]:
            if line.split()[0] == part.node:
                fail("MOUNT", "target is active swap")
        info = os.stat(part.node)
        device = Path("/sys/dev/block") / f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        if any((device / "holders").iterdir()):
            fail("MOUNT", "target has active device-mapper/RAID holders")

    @contextmanager
    def provider_lock(self):
        import fcntl
        path = Path(self.config.socket_path).parent / "provider.lock"
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError("PROVIDER_ACTIVE", "provider launch interlock is held") from exc
            self.provider_idle()
            yield
        finally:
            os.close(fd)

    def mount_info(self, target):
        result = self.run(["findmnt", "--json", "--mountpoint", str(target),
                           "--output", "SOURCE,MAJ:MIN,TARGET,FSTYPE"])
        try:
            mounts = json.loads(result.stdout)["filesystems"]
            if len(mounts) != 1 or mounts[0]["target"] != str(target):
                fail("MOUNT", f"exact mount required: {target}")
            return mounts[0]
        except (KeyError, ValueError, TypeError) as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("MOUNT", f"invalid findmnt result for {target}") from exc

    def ancestry(self, major_minor):
        if not re.fullmatch(r"[0-9]+:[0-9]+", major_minor):
            fail("LAYOUT", "invalid block identity")
        path = (Path("/sys/dev/block") / major_minor).resolve(strict=True)
        number = int((path / "partition").read_text().strip())
        disk = "/dev/" + path.parent.name
        return disk, number

    def inspect(self):
        return self._inspect()

    def inspect_early(self, layout_record):
        return self._inspect(layout_record=layout_record)

    def _inspect(self, *, layout_record=None):
        root, boot = (self.mount_info(p) for p in ("/", "/boot/firmware"))
        if (root["fstype"], boot["fstype"]) != ("ext4", "vfat"):
            fail("LAYOUT", "unexpected active filesystems")
        disk, root_number = self.ancestry(root["maj:min"])
        boot_disk, boot_number = self.ancestry(boot["maj:min"])
        if disk != boot_disk:
            fail("LAYOUT", "root/boot/data do not share the same physical disk")
        if layout_record is None:
            data = self.mount_info("/data")
            data_disk, data_number = self.ancestry(data["maj:min"])
            if disk != data_disk or data_number != 6 or data["fstype"] != "ext4":
                fail("LAYOUT", "root/boot/data do not share the same physical disk")
        output = self.run(["sfdisk", "--json", disk]).stdout
        try:
            table = json.loads(output)["partitiontable"]
            layout = validate_table(table, disk=disk, root_number=root_number,
                                    boot_number=boot_number)
        except (KeyError, json.JSONDecodeError) as exc:
            raise UpdateError("LAYOUT", "invalid partition table") from exc
        if layout_record is None:
            record = Path("/data/cloudplay/layout.json")
            trusted_path(record)
            layout_record = read_json(record, 4096)
        validate_layout_record(layout_record, table)
        for part in layout.partitions:
            info = os.stat(part.node, follow_symlinks=False)
            if not stat.S_ISBLK(info.st_mode):
                fail("LAYOUT", "partition path is not a block device")
            actual = self.ancestry(f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")
            if actual != (disk, part.number):
                fail("LAYOUT", "GPT node does not match physical disk/partition number")
            uuid = self.run(["blkid", "-s", "PARTUUID", "-o", "value", part.node]).stdout.strip().lower()
            size = int(self.run(["blockdev", "--getsize64", part.node]).stdout)
            if uuid != part.uuid or size != part.size:
                fail("LAYOUT", "kernel/GPT UUID or size disagreement")
        return layout

    def provider_idle(self):
        result = self.run(["systemctl", "list-units", "--all", "--plain", "--no-legend",
                           "--no-pager", "cloudplay-provider-*", "cloudplay-stream.service"])
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            columns = line.split(None, 4)
            if len(columns) < 4:
                fail("COMMAND", "invalid systemd provider-unit state")
            if columns[2] not in ("inactive", "failed"):
                fail("PROVIDER_ACTIVE", "a provider transient unit is running")
        # Parent must put browser processes in system-level provider units. UID
        # scanning also covers user units, stale browsers and hand-launched sessions.
        result = self.run(["pgrep", "-u", str(self.config.browser_uid)],
                          allowed=(0, 1))
        if result.returncode == 0:
            fail("PROVIDER_ACTIVE", "browser identity still has running processes")

    def precheck(self):
        self.config.mutation_gate()
        self.provider_idle()
        layout = self.inspect()
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", self.config.minimum_eeprom):
            fail("EEPROM", "reviewed minimum EEPROM policy absent")
        version = self.run(["vcgencmd", "bootloader_version"]).stdout
        timestamp = re.search(r"timestamp\s+([0-9]+)", version)
        if not timestamp or time.strftime("%Y-%m-%d", time.gmtime(int(timestamp[1]))) < self.config.minimum_eeprom:
            fail("EEPROM", "bootloader below reviewed minimum")
        config = self.run(["vcgencmd", "bootloader_config"]).stdout
        orders = re.findall(r"^BOOT_ORDER=(0x[0-9a-fA-F]+)$", config, re.MULTILINE)
        if not self.config.boot_order or orders != [self.config.boot_order]:
            fail("EEPROM", "BOOT_ORDER differs from reviewed policy")
        if self.run(["vcgencmd", "get_throttled"]).stdout.strip() != "throttled=0x0":
            fail("POWER", "undervoltage/throttling reported")
        if self.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).stdout.strip() != "yes":
            fail("CLOCK", "HTTPS clock synchronization not established")
        probe = Path(self.config.state_dir) / ".write-probe"
        atomic_write(probe, b"writable\n")
        probe.unlink()
        sync_directory(probe.parent)
        return layout

    @contextmanager
    def mounted(self, part: Partition, name: str):
        self.config.mutation_gate()
        self.unmounted(part)
        self.work.mkdir(mode=0o700, exist_ok=True)
        if self.work.is_symlink():
            fail("MOUNT", "private mount directory is a symlink")
        target = self.work / name
        target.mkdir(mode=0o700, exist_ok=True)
        if target.is_symlink() or any(target.iterdir()):
            fail("MOUNT", "private mountpoint must be empty")
        options = "nodev,nosuid,noexec"
        if part.number <= 3:
            options += ",uid=0,gid=0,fmask=0133,dmask=0022"
        self.mutate(["mount", "-o", options, part.node, str(target)])
        try:
            info = self.mount_info(target)
            major_minor = os.stat(part.node).st_rdev
            if info["maj:min"] != f"{os.major(major_minor)}:{os.minor(major_minor)}":
                fail("MOUNT", "mounted wrong block device")
            yield target
        finally:
            self.mutate(["umount", str(target)])

    def flush(self, path):
        self.mutate(["sync", "-f", str(path)], timeout=120)

    def _generated(self, layout, metadata, root, boot):
        slot = layout.target
        root_id, boot_id = layout.part(ROOT[slot]).uuid, layout.part(BOOT[slot]).uuid
        fstab = (
            "proc /proc proc defaults 0 0\n"
            f"PARTUUID={root_id} / ext4 defaults,noatime 0 1\n"
            f"PARTUUID={boot_id} /boot/firmware vfat defaults,uid=0,gid=0,fmask=0133,dmask=0022 0 2\n"
        ).encode()
        cmdline = render_cmdline(root, metadata, root_id)
        files = {"root/etc/fstab": (fstab, 0o644),
                 "boot/cmdline.txt": (cmdline, 0o644)}
        if "boot/autoboot.txt" not in GENERATED_PATHS:
            fail("ARTIFACT_CONTRACT", "closed boot/autoboot.txt exemption required")
        files["boot/autoboot.txt"] = (self.pointer(layout.active, slot), 0o644)
        files.update(identity_files(Path("/data/cloudplay/identity"), Path("/etc")))
        from .boot import make_ticket
        previous = read_json(Path("/boot/firmware/slot-valid.json"), 4096)
        previous = {key: previous[key] for key in ("slot", "version", "manifest_sha256")}
        if previous["slot"] != layout.active:
            fail("IDENTITY", "active sentinel does not match the source slot")
        previous["config_sha256"] = sha256_file(Path("/boot/firmware/config.txt"))
        sentinel = dict(schema=1, slot=slot, version=metadata["version"],
                        manifest_sha256=metadata["manifest_sha256"],
                        guard=make_ticket(self.config, read_json(
                            Path("/data/cloudplay/layout.json"), 4096), previous))
        files["boot/slot-valid.json"] = (canonical_json(sentinel), 0o644)
        generated = {}
        for name, (data, mode) in files.items():
            prefix, relative = name.split("/", 1)
            destination = safe_child(root if prefix == "root" else boot, relative)
            atomic_write(destination, data, mode)
            record = file_record(data, mode=mode)
            if prefix == "root":
                _set_attributes(destination, record)
            generated[name] = record
        return generated

    def snapshot_profiles(self, layout):
        """Slot-local copies prevent tentative Chromium from mutating last-good."""
        self.config.mutation_gate()
        self.provider_idle()
        base = Path(self.config.profile_root)
        source, target = base / layout.active, base / layout.target
        if base.is_symlink() or not base.is_dir() or source.is_symlink() or not source.is_dir():
            fail("PROFILE", "seeded per-slot profiles are required")
        if source.stat().st_uid != self.config.browser_uid:
            fail("PROFILE", "active profiles are not owned by the isolated browser identity")
        info = self.mount_info(Path(self.config.profile_mount))
        if info["maj:min"] != self.mount_info("/data")["maj:min"]:
            fail("PROFILE", "profile mount is not on persistent data")
        if os.stat(source).st_ino != os.stat(self.config.profile_mount).st_ino:
            fail("PROFILE", "active profile mount maps to the wrong slot")
        temporary = base / (layout.target + ".new")
        if temporary.exists() or temporary.is_symlink():
            fail("PROFILE", "interrupted profile snapshot requires operator cleanup")
        # cp -a preserves numeric IDs, xattrs and links without following them.
        # Refuse special files and require safe relative links in user profiles.
        for parent, dirs, names in os.walk(source, followlinks=False):
            for name in dirs + names:
                path = Path(parent) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    link = os.readlink(path)
                    if Path(link).is_absolute() or not (path.parent / link).resolve().is_relative_to(source.resolve()):
                        fail("PROFILE", "profile has an escaping symlink (close Chromium first)")
                elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    fail("PROFILE", "profile contains a live socket or special file")
        self.mutate(["cp", "-a", "--reflink=auto", "--", str(source), str(temporary)], timeout=600)
        self.provider_idle()
        self.flush(temporary)
        if target.is_symlink():
            fail("PROFILE", "target profile is a symlink")
        if target.exists():
            self.mutate(["rm", "-r", "--one-file-system", "--", str(target)], timeout=300)
        os.rename(temporary, target)
        sync_directory(base)

    def stage(self, source: Path, metadata: dict, layout: Layout, checkpoint, *,
              progress=None, activity=None):
        """Write only verified inactive partitions; config.txt is the final gate."""
        self.config.mutation_gate()
        if self.inspect() != layout:
            fail("LAYOUT_CHANGED", "mounted disk changed after prechecks")
        self.provider_idle()
        verify_tree(source, metadata, activity=activity, operation="check_source")
        for name, record in metadata["manifest"].items():
            if name == "boot" or name.startswith("boot/"):
                if (record["type"] == "symlink" or record["uid"] != 0 or record["gid"] != 0
                        or record["mode"] != (0o755 if record["type"] == "directory" else 0o644)):
                    fail("BOOT_METADATA", "FAT requires root-owned 0755 directories/0644 regular files")
        config_record = metadata["manifest"].get("boot/config.txt")
        if not config_record or config_record["type"] != "file":
            fail("BOOT_CONFIG", "signed regular config.txt is mandatory")
        if "boot/tryboot.txt" in metadata["manifest"]:
            fail("BOOT_CONFIG", "alternate tryboot.txt unsupported; remove from bundle")
        render_cmdline(source / "root", metadata, layout.part(ROOT[layout.target]).uuid)
        checkpoint("invalidating", None)
        if activity:
            activity("prepare_storage")
        target_boot, target_root = layout.part(BOOT[layout.target]), layout.part(ROOT[layout.target])
        # Removing both entrypoints is mandatory before ANY target-root mutation.
        with self.mounted(target_boot, "boot") as boot:
            for name in ("config.txt", "tryboot.txt", "slot-valid.json"):
                safe_child(boot, name).unlink(missing_ok=True)
            self.flush(boot)
            if any((boot / name).exists() for name in ("config.txt", "tryboot.txt")):
                fail("BOOT_GATE", "inactive boot entrypoint remains present")
        if activity:
            activity("preserve_profiles")
        self.snapshot_profiles(layout)
        checkpoint("staging_boot", None)
        if activity:
            activity("prepare_storage")
        self.unmounted(target_boot)
        self.mutate(["mkfs.vfat", "-F", "32", "-n", f"CP-BOOT-{layout.target}", target_boot.node], timeout=120)
        with self.mounted(target_boot, "boot") as boot:
            copy_payload(source, boot, metadata, "boot", frozenset({"boot/config.txt"}),
                         progress=(lambda received, total: progress("staging_boot", received, total))
                         if progress else None)
            if activity:
                activity("save_boot")
            self.flush(boot)
            checkpoint("staging_root", None)
            if activity:
                activity("prepare_storage")
            self.unmounted(target_root)
            self.mutate(["mkfs.ext4", "-F", "-m", "0", "-L", f"root-{layout.target}", target_root.node], timeout=300)
            with self.mounted(target_root, "root") as root:
                # mkfs creates lost+found; it is not a signed payload member.
                lost = root / "lost+found"
                if lost.is_dir() and not any(lost.iterdir()):
                    lost.rmdir()
                copy_payload(source, root, metadata, "root",
                             progress=(lambda received, total: progress("staging_root", received, total))
                             if progress else None)
                if activity:
                    activity("configure_system")
                generated = self._generated(layout, metadata, root, boot)
                checkpoint("verifying_slot", generated)
                verify_slot(root, boot, metadata, generated, frozenset({"boot/config.txt"}),
                            activity=activity)
                if activity:
                    activity("save_system")
                self.flush(root)
                self.flush(boot)
                # Durable pending identity precedes the last firmware-visible write.
                checkpoint("publishing", generated)
                if activity:
                    activity("save_boot")
                destination = safe_child(boot, "config.txt")
                atomic_write(destination, (source / "boot" / "config.txt").read_bytes(), 0o644)
                self.flush(boot)
                verify_slot(root, boot, metadata, generated, activity=activity,
                            operation="check_final")
                if activity:
                    activity("release_storage")
        checkpoint("ready_to_restart", generated)

    @staticmethod
    def pointer(default, candidate):
        return (f"[all]\ntryboot_a_b=1\nboot_partition={BOOT[default]}\n"
                f"[tryboot]\nboot_partition={BOOT[candidate]}\n").encode()

    def write_pointers(self, layout, default, candidate):
        self.config.mutation_gate()
        if "boot/autoboot.txt" not in GENERATED_PATHS:
            fail("ARTIFACT_CONTRACT", "boot/autoboot.txt needs a closed generated-file exemption before pointer writes")
        data = self.pointer(default, candidate)
        for slot in ("A", "B"):
            if slot == layout.active:
                path = Path("/boot/firmware")
                atomic_write(path / "autoboot.txt", data, 0o644)
                self.flush(path)
                if (path / "autoboot.txt").read_bytes() != data:
                    fail("POINTER", "active mirror readback mismatch")
            else:
                with self.mounted(layout.part(BOOT[slot]), "mirror") as path:
                    atomic_write(path / "autoboot.txt", data, 0o644)
                    self.flush(path)
                    if (path / "autoboot.txt").read_bytes() != data:
                        fail("POINTER", "inactive mirror readback mismatch")
        with self.mounted(layout.part(1), "control") as path:
            atomic_write(path / "autoboot.txt", data, 0o644)
            self.flush(path)
            if (path / "autoboot.txt").read_bytes() != data:
                fail("POINTER", "authoritative pointer readback mismatch")

    def verify_candidate(self, layout, pending, *, activity=None):
        if pending["slot"] == layout.active:
            fail("SLOT", "candidate readback requires inactive slot")
        with self.mounted(layout.part(ROOT[pending["slot"]]), "root") as root:
            with self.mounted(layout.part(BOOT[pending["slot"]]), "boot") as boot:
                verify_slot(root, boot, pending["metadata"], pending["generated"],
                            activity=activity, operation="check_restart")

    def verify_good(self, layout, identity):
        slot = identity["slot"]
        def check(path):
            sentinel = read_json(path / "slot-valid.json", 4096)
            if any(sentinel.get(k) != identity[k] for k in ("slot", "version", "manifest_sha256")):
                fail("RECOVERY", "last-good sentinel identity mismatch; reflash/operator recovery required")
            if sha256_file(path / "config.txt") != identity["config_sha256"]:
                fail("RECOVERY", "last-good firmware config invalid")
        if slot == layout.active:
            check(Path("/boot/firmware"))
        else:
            with self.mounted(layout.part(BOOT[slot]), "good") as boot:
                check(boot)

    def quarantine_running(self):
        self.config.mutation_gate()
        boot = Path("/boot/firmware")
        for name in ("config.txt", "tryboot.txt"):
            safe_child(boot, name).unlink(missing_ok=True)
        self.flush(boot)

    def guard_lock(self):
        from .boot import guard_lock
        return guard_lock()

    def guard_state(self, pending=None, *, boot_dir=None):
        from .boot import BOOT_DIR, read_sentinel, validate_ticket
        sentinel = read_sentinel(BOOT_DIR if boot_dir is None else boot_dir)
        parsed = validate_ticket(sentinel)
        if parsed is None:
            fail("BOOT_GUARD", "durable candidate has no early-boot capability")
        if pending is not None and (
                sentinel["slot"] != pending["slot"]
                or any(sentinel[key] != pending["metadata"][key]
                       for key in ("version", "manifest_sha256"))):
            fail("BOOT_GUARD", "candidate capability does not match durable pending identity")
        return parsed[0]

    def candidate_guard_attempted(self, layout, pending):
        if pending["slot"] == layout.active:
            fail("SLOT", "inactive candidate guard requested for running slot")
        with self.mounted(layout.part(BOOT[pending["slot"]]), "guard") as boot:
            return self.guard_state(pending, boot_dir=boot)["attempted"]

    def mark_guard(self, *, confirmed=False):
        from .boot import read_sentinel, validate_ticket, write_sentinel
        self.config.mutation_gate()
        value = read_sentinel()
        parsed = validate_ticket(value)
        if parsed is None:
            fail("BOOT_GUARD", "candidate capability is absent")
        ticket, _ = parsed
        if ticket["attempted"]:
            fail("ROLLBACK_LOOP", "boot-local rollback already attempted")
        if confirmed:
            ticket.update(armed=False, confirmed=True)
        else:
            ticket["attempted"] = True
        write_sentinel(self, value)

    @contextmanager
    def inhibitor(self):
        """The ready byte is emitted only after logind grants the inhibitor."""
        import selectors
        self.config.mutation_gate()
        process = subprocess.Popen(
            ["systemd-inhibit", "--what=shutdown:sleep", "--mode=block",
             "--who=cloudplay-updater", "--why=Inactive slot write in progress",
             "/usr/bin/python3", "-c",
             "import sys,time; print('ready',flush=True); time.sleep(1800)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(10) or os.read(process.stdout.fileno(), 16) != b"ready\n":
                    fail("INHIBITOR", "logind shutdown inhibitor not acquired")
            yield lambda: process.poll() is None
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # The bounded inhibitor command has already exited.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            process.stdout.close()

    def reboot(self, *, tryboot=False):
        self.mutate(["reboot", "0 tryboot"] if tryboot else ["systemctl", "reboot"], timeout=30)

    def health(self, pending, *, now=None):
        layout = self.inspect()
        if layout.active != pending["slot"]:
            fail("HEALTH", "running slot differs from durable candidate")
        release = read_json(Path(self.config.release_file))
        sentinel = read_json(Path("/boot/firmware/slot-valid.json"), 4096)
        metadata = pending["metadata"]
        # The release record cannot contain its own enclosing manifest digest.
        # Instead verify its signed entry, and bind the sentinel to the digest.
        record = metadata["manifest"].get("root/usr/share/cloudplay/release.json")
        if (not record or record.get("type") != "file"
                or sha256_file(Path(self.config.release_file)) != record["sha256"]
                or release.get("version") != metadata["version"]
                or release.get("source_commit") != metadata["source_commit"]
                or sha256_file(Path("/boot/firmware/config.txt")) != metadata["manifest"]["boot/config.txt"]["sha256"]
                or any(sentinel.get(key) != metadata[key] for key in ("version", "manifest_sha256"))):
            fail("HEALTH", "signed release record or sentinel differs from candidate")
        if sentinel.get("slot") != layout.active or release.get("launcher_smoke_passed") is not True:
            fail("HEALTH", "slot identity or baked launcher smoke check failed")
        profile = Path(self.config.profile_root) / layout.active
        mounted = self.mount_info(Path(self.config.profile_mount))
        if (mounted["maj:min"] != self.mount_info("/data")["maj:min"]
                or os.stat(profile).st_ino != os.stat(self.config.profile_mount).st_ino
                or profile.stat().st_uid != self.config.browser_uid):
            fail("HEALTH", "persistent profile mapping/ownership invalid")
        for unit in HEALTH_UNITS:
            if self.run(["systemctl", "is-active", unit], allowed=(0, 3)).stdout.strip() != "active":
                fail("HEALTH", f"{unit} is not active")
        current = time.monotonic() if now is None else now
        boot_id = self.boot_id()
        if boot_id != pending["boot_id"]:
            fail("HEALTH", "candidate boot identity changed without reconciliation")
        heartbeat_dir = Path("/run/cloudplay-update-ui")
        info = heartbeat_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.config.launcher_uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            fail("HEALTH", "trusted compositor runtime ownership/mode mismatch")
        for name in ("launcher", "compositor"):
            path = heartbeat_dir / (name + "-heartbeat.json")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.config.launcher_uid
                    or stat.S_IMODE(info.st_mode) != 0o600):
                fail("HEALTH", "heartbeat is not owned by the isolated update identity")
            heartbeat = read_json(path, 4096)
            if (heartbeat.get("boot_id") != boot_id or type(heartbeat.get("monotonic")) not in (int, float)
                    or not 0 <= current - heartbeat["monotonic"] <= 30):
                fail("HEALTH", f"missing/stale {name} heartbeat")
        probe = Path(self.config.state_dir) / ".health-write"
        atomic_write(probe, b"ok")
        probe.unlink()
        return layout
