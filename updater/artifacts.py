"""Signed OTA artifacts. Extraction is staging-only, never a partition writer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import total_ordering
import base64
import binascii
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import threading


MAX_COMPRESSED = 4 * 1024**3
MAX_EXPANDED = 8 * 1024**3
MAX_METADATA = 32 * 1024**2
MAX_CATALOG = 16384
MAX_SIGNATURE = 8192
MAX_MEMBERS = 200000
MAX_PATH = 4096
MAX_ZSTD_MEMORY = "128MB"
BOOT_LIMIT = 768 * 1024**2
ROOT_LIMIT = 6 * 1024**3
MAX_XATTR_BYTES = 65536
SCHEMA = 1
GENERATED_PATHS = frozenset({
    "boot/cmdline.txt", "boot/slot-valid.json", "boot/autoboot.txt", "root/etc/fstab",
    "root/etc/machine-id",
    *(f"root/etc/ssh/ssh_host_{kind}_key{suffix}"
      for kind in ("rsa", "ecdsa", "ed25519") for suffix in ("", ".pub")),
})
_HEX = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[0-9a-f]{40}$")
_KEY = re.compile(r"^epoch-([1-9][0-9]*)-[a-zA-Z0-9_-]+\.pub$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ArtifactError(ValueError):
    """A fail-closed artifact error; ``code`` is suitable for status/logging."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code, message):
    raise ArtifactError(code, message)


@total_ordering
@dataclass(frozen=True, eq=False)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        if not isinstance(value, str) or len(value) > 128:
            _fail("VERSION", "invalid SemVer")
        match = _SEMVER.fullmatch(value)
        if not match:
            _fail("VERSION", "invalid SemVer")
        pre = tuple(match[4].split(".")) if match[4] else ()
        if any(p.isdigit() and len(p) > 1 and p[0] == "0" for p in pre):
            _fail("VERSION", "numeric prerelease has a leading zero")
        return cls(int(match[1]), int(match[2]), int(match[3]), pre, match[5] or "")

    def __eq__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        return (self.major, self.minor, self.patch, self.prerelease) == (
            other.major, other.minor, other.patch, other.prerelease)

    def __hash__(self):
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __lt__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        left, right = (self.major, self.minor, self.patch), (
            other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for a, b in zip(self.prerelease, other.prerelease):
            if a == b:
                continue
            if a.isdigit() and b.isdigit():
                return int(a) < int(b)
            if a.isdigit() != b.isdigit():
                return a.isdigit()
            return a < b
        return len(self.prerelease) < len(other.prerelease)


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("METADATA", "duplicate JSON key")
        result[key] = value
    return result


def _json(data: bytes, limit: int):
    if len(data) > limit:
        _fail("LIMIT", "JSON document exceeds limit")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda _: _fail("METADATA", "nonfinite JSON"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        _fail("METADATA", "invalid JSON")
    if not isinstance(result, dict):
        _fail("METADATA", "expected JSON object")
    return result


def _regular(path: Path, maximum: int) -> int:
    try:
        info = path.lstat()
    except OSError as exc:
        _fail("IO", f"required artifact unavailable: {path.name}: {exc}")
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_size == 0:
        _fail("LIMIT", f"invalid artifact file: {path.name}")
    return info.st_size


def _read(path: Path, limit: int) -> bytes:
    _regular(path, limit)
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        _fail("LIMIT", "file grew past size limit")
    return data


def _integer(value, low=0, high=MAX_EXPANDED):
    return type(value) is int and low <= value <= high


def _xattrs(value):
    if not isinstance(value, dict) or len(value) > 32:
        _fail("ATTRIBUTES", "invalid extended attribute set")
    total = 0
    for name, encoded in value.items():
        if (not isinstance(name, str) or len(name) > 255 or
                not (name in ("security.capability", "system.posix_acl_access",
                              "system.posix_acl_default") or
                     re.fullmatch(r"user\.[A-Za-z0-9_.-]+", name)) or
                not isinstance(encoded, str) or len(encoded) > 4 * MAX_XATTR_BYTES // 3 + 4):
            _fail("ATTRIBUTES", "unsupported extended attribute")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _fail("ATTRIBUTES", "invalid extended attribute encoding")
        total += len(decoded)
        if total > MAX_XATTR_BYTES or base64.b64encode(decoded).decode("ascii") != encoded:
            _fail("ATTRIBUTES", "extended attribute size/encoding mismatch")
    return value


def read_xattrs(path: Path):
    """Read only supported target metadata, without following a payload link."""
    if os.name != "posix":
        return {}
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno != errno.ENOTSUP:
            raise
        return {}
    if len(names) > 32:
        _fail("ATTRIBUTES", "too many extended attributes")
    return _xattrs({
        name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode("ascii")
        for name in names
    })


def _path(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > MAX_PATH
            or "\\" in name or "\x00" in name
            or any(ord(c) < 32 for c in name)):
        _fail("ARCHIVE", "invalid member path")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        _fail("ARCHIVE", "noncanonical member path")
    if parts[0] not in ("boot", "root"):
        _fail("ARCHIVE", "member outside boot/root")
    if parts[0] == "boot" and any(
            p.endswith((" ", ".")) or any(c in p for c in ':*?"<>|') for p in parts):
        _fail("ARCHIVE", "unrepresentable FAT member path")
    return name


def _host_path(name: str):
    if os.name == "nt" and any(
            ":" in p or p.endswith((" ", ".")) or
            re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", p)
            for p in name.split("/")):
        _fail("ARCHIVE", "Linux member requires a POSIX staging filesystem")


def _link(name: str, target: str):
    if (not isinstance(target, str) or not target or len(target) > MAX_PATH
            or "\\" in target or ":" in target or "\x00" in target
            or any(ord(c) < 32 for c in target) or target.startswith("//")):
        _fail("ARCHIVE", "invalid symlink target")
    # Absolute Unix links are target-root links, never host paths to follow.
    stack = [] if target.startswith("/") else name.split("/")[1:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                _fail("ARCHIVE", "symlink escapes its target filesystem")
            stack.pop()
        else:
            stack.append(part)


def validate_metadata(meta: dict, *, platform: str, channel: str,
                      current_version: str, highest_version: str,
                      minimum_key_epoch: int, data_schema: int) -> dict:
    required = {"schema", "version", "source_commit", "minimum_source_version",
                "platform", "channel", "key_epoch", "created_at", "workflow",
                "data_schema_min", "data_schema_max", "manifest",
                "manifest_sha256", "payload_bytes"}
    if not isinstance(meta, dict) or set(meta) != required or type(meta.get("schema")) is not int or meta["schema"] != SCHEMA:
        _fail("METADATA", "unsupported metadata fields/schema")
    target = SemVer.parse(meta["version"])
    minimum = SemVer.parse(meta["minimum_source_version"])
    current, highest = SemVer.parse(current_version), SemVer.parse(highest_version)
    if target <= current or target < highest or current < minimum:
        _fail("VERSION", "version is not an eligible upgrade")
    if (meta["platform"] != platform or channel not in ("stable", "beta")
            or meta["channel"] not in ("stable", "beta")
            or channel == "stable" and (meta["channel"] != "stable" or target.prerelease)
            or meta["channel"] == "stable" and target.prerelease):
        _fail("COMPATIBILITY", "platform/channel mismatch")
    if (not _integer(minimum_key_epoch, 1) or not _integer(meta["key_epoch"], 1)
            or meta["key_epoch"] < minimum_key_epoch):
        _fail("KEY_EPOCH", "signing epoch below trusted floor")
    if (not _integer(data_schema) or not _integer(meta["data_schema_min"])
            or not _integer(meta["data_schema_max"])
            or not meta["data_schema_min"] <= data_schema <= meta["data_schema_max"]):
        _fail("COMPATIBILITY", "unsupported persistent-data schema")
    if (not isinstance(meta["source_commit"], str)
            or not _SOURCE.fullmatch(meta["source_commit"])
            or not isinstance(meta["created_at"], str)
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["created_at"])
            or not isinstance(meta["workflow"], str) or not 1 <= len(meta["workflow"]) <= 256
            or not _integer(meta["payload_bytes"], 0, MAX_EXPANDED)):
        _fail("METADATA", "invalid provenance/size")
    try:
        datetime.strptime(meta["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail("METADATA", "invalid creation timestamp")
    manifest = meta["manifest"]
    if not isinstance(manifest, dict) or not 2 <= len(manifest) <= MAX_MEMBERS:
        _fail("LIMIT", "invalid manifest size")
    if hashlib.sha256(canonical_json(manifest)).hexdigest() != meta["manifest_sha256"]:
        _fail("MANIFEST", "manifest digest mismatch")
    folded = set()
    sizes = {"boot": 0, "root": 0}
    for name, record in manifest.items():
        _path(name)
        if name in GENERATED_PATHS:
            _fail("MANIFEST", "generated files must be absent from the bundle")
        if name.startswith("boot/") and name.casefold() in folded:
            _fail("MANIFEST", "case-colliding member names")
        folded.add(name.casefold())
        if not isinstance(record, dict):
            _fail("MANIFEST", "invalid entry")
        kind = record.get("type")
        fields = {"type", "mode", "uid", "gid"}
        if "xattrs" in record:
            fields.add("xattrs")
            if name == "boot" or name.startswith("boot/"):
                _fail("ATTRIBUTES", "FAT payload cannot carry extended attributes")
            _xattrs(record["xattrs"])
        if kind == "file":
            fields |= {"size", "sha256"}
            if (not _integer(record.get("size")) or not isinstance(record.get("sha256"), str)
                    or not _HEX.fullmatch(record["sha256"])):
                _fail("MANIFEST", "invalid file size/hash")
            sizes[name.split("/")[0]] += record["size"]
        elif kind == "symlink":
            if name.startswith("boot/"):
                _fail("MANIFEST", "FAT payload cannot contain symlinks")
            fields.add("target")
            _link(name, record.get("target"))
        elif kind != "directory":
            _fail("MANIFEST", "unsupported entry type")
        if (set(record) != fields or not _integer(record.get("mode"), 0, 0o7777)
                or not _integer(record.get("uid"), 0, 2**32 - 2)
                or not _integer(record.get("gid"), 0, 2**32 - 2)):
            _fail("MANIFEST", "invalid entry fields/ownership")
        if name in ("boot", "root") and kind != "directory":
            _fail("MANIFEST", "filesystem root is not a directory")
    if sizes["boot"] > BOOT_LIMIT or sizes["root"] > ROOT_LIMIT:
        _fail("LIMIT", "payload exceeds 75% slot budget")
    if sizes["boot"] + sizes["root"] != meta["payload_bytes"]:
        _fail("MANIFEST", "payload byte count mismatch")
    for name in manifest:
        parts = name.split("/")
        for count in range(1, len(parts)):
            if manifest.get("/".join(parts[:count]), {}).get("type") != "directory":
                _fail("MANIFEST", "missing/non-directory member parent")
    if not {"boot", "root"} <= manifest.keys():
        _fail("MANIFEST", "missing filesystem roots")
    return meta


def _verify_signature(message: Path, signature: Path, key: Path) -> bool:
    try:
        result = subprocess.run(
            ["minisign", "-Vm", str(message), "-x", str(signature), "-p", str(key)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("SIGNATURE_TOOL", f"minisign unavailable/failed: {exc}")
    return result.returncode == 0


def _trust(bundle, signature, keys_dir, minimum_key_epoch):
    size = _regular(bundle, MAX_COMPRESSED)
    _regular(signature, MAX_SIGNATURE)
    catalog_path = bundle.with_name(bundle.name + ".catalog.json")
    catalog_sig = catalog_path.with_name(catalog_path.name + ".minisig")
    _regular(catalog_path, MAX_CATALOG)
    _regular(catalog_sig, MAX_SIGNATURE)
    epoch = None
    for key in sorted(keys_dir.glob("epoch-*.pub")):
        match = _KEY.fullmatch(key.name)
        if not match or int(match[1]) < minimum_key_epoch:
            continue
        _regular(key, MAX_SIGNATURE)
        if _verify_signature(catalog_path, catalog_sig, key) and _verify_signature(bundle, signature, key):
            epoch = int(match[1])
            break
    if epoch is None:
        _fail("SIGNATURE", "no trusted key verified catalog and compressed bundle")
    catalog = _json(_read(catalog_path, MAX_CATALOG), MAX_CATALOG)
    expected = {"schema", "name", "version", "source_commit", "platform", "channel",
                "key_epoch", "compressed_size", "sha256", "required_staging_bytes",
                "uncompressed_size"}
    if (set(catalog) != expected or type(catalog["schema"]) is not int or catalog["schema"] != SCHEMA
            or catalog["name"] != bundle.name or not _integer(catalog["key_epoch"], 1)
            or catalog["key_epoch"] != epoch
            or not _integer(catalog["compressed_size"], 1, MAX_COMPRESSED)
            or catalog["compressed_size"] != size
            or not _integer(catalog["uncompressed_size"], 1024, MAX_EXPANDED)
            or not _integer(catalog["required_staging_bytes"], 1, 4 * MAX_EXPANDED)
            or catalog["required_staging_bytes"] < catalog["uncompressed_size"] * 2 + size
            or catalog["sha256"] != sha256_file(bundle)):
        _fail("CATALOG", "signed catalog does not bind this compressed artifact")
    return catalog


def _decompress(bundle: Path, archive: Path, *, maximum=None):
    maximum = MAX_EXPANDED if maximum is None else min(maximum, MAX_EXPANDED)
    try:
        with archive.open("xb") as output:
            process = subprocess.Popen(
                ["zstd", "-d", "--stdout", "--no-progress",
                 f"--memory={MAX_ZSTD_MEMORY}", "--", str(bundle)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            timer = threading.Timer(600, process.kill)
            timer.daemon = True
            timer.start()
            total = 0
            try:
                while True:
                    chunk = process.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        _fail("LIMIT", "decompressed archive exceeds limit")
                    output.write(chunk)
                if process.wait() != 0:
                    _fail("DECOMPRESS", "zstd rejected stream or resource limit")
                output.flush()
                os.fsync(output.fileno())
            finally:
                timer.cancel()
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()
    except OSError as exc:
        _fail("DECOMPRESS", f"zstd unavailable or staging IO failure: {exc}")


def _scan_tar(archive: Path):
    """Bound extension headers before tarfile can allocate for them."""
    size = archive.stat().st_size
    count = 0
    extensions = 0
    with archive.open("rb") as source:
        while source.tell() < size:
            header = source.read(512)
            if len(header) != 512:
                _fail("ARCHIVE", "truncated tar header")
            if header == b"\0" * 512:
                # Only zero padding can follow end-of-archive (no hidden second tar).
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    if chunk.strip(b"\0"):
                        _fail("ARCHIVE", "nonzero trailing archive data")
                return
            try:
                info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            except (tarfile.TarError, ValueError, UnicodeError):
                _fail("ARCHIVE", "invalid tar header")
            count += 1
            if count > MAX_MEMBERS * 2 + 8 or info.size < 0:
                _fail("LIMIT", "too many tar headers")
            if info.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                             tarfile.GNUTYPE_LONGLINK):
                extensions += 1
                if extensions > 2:
                    _fail("ARCHIVE", "too many consecutive extension headers")
                if info.size > MAX_PATH * 4 or info.type == tarfile.XGLTYPE:
                    _fail("ARCHIVE", "unsupported/oversized extension header")
                extension = source.read(info.size)
                if b"GNU.sparse" in extension or b"SCHILY." in extension:
                    _fail("ARCHIVE", "sparse/xattr extension is unsupported")
                source.seek((-info.size) % 512, 1)
            else:
                extensions = 0
                if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE):
                    _fail("ARCHIVE", "special archive member is forbidden")
                source.seek(((info.size + 511) // 512) * 512, 1)
            if source.tell() > size:
                _fail("ARCHIVE", "member extends past archive")
    _fail("ARCHIVE", "missing tar end marker")


def _record(info):
    record = {"mode": info.mode, "uid": info.uid, "gid": info.gid}
    if info.isdir():
        record["type"] = "directory"
    elif info.isreg():
        record.update(type="file", size=info.size)
    elif info.issym():
        record.update(type="symlink", target=info.linkname)
    else:
        _fail("ARCHIVE", "devices, FIFOs, hardlinks and special members are forbidden")
    return record


def _set_attributes(path: Path, record: dict):
    if os.name != "posix":
        return  # Windows tests verify content; production preservation requires Linux.
    if os.geteuid() != 0:
        info = path.lstat()
        if (info.st_uid, info.st_gid) != (record["uid"], record["gid"]):
            _fail("OWNERSHIP", "numeric ownership preservation requires root")
    else:
        os.chown(path, record["uid"], record["gid"], follow_symlinks=False)
    if record["type"] != "symlink":
        os.chmod(path, record["mode"], follow_symlinks=False)
    expected = record.get("xattrs", {})
    for name in read_xattrs(path).keys() - expected.keys():
        os.removexattr(path, name, follow_symlinks=False)
    for name, encoded in expected.items():
        os.setxattr(path, name, base64.b64decode(encoded, validate=True), follow_symlinks=False)


def verify_attributes(path: Path, record: dict):
    if os.name != "posix":
        return
    info = path.lstat()
    if (info.st_uid, info.st_gid) != (record["uid"], record["gid"]):
        _fail("OWNERSHIP", "ownership mismatch")
    if record["type"] != "symlink" and stat.S_IMODE(info.st_mode) != record["mode"]:
        _fail("MANIFEST", "permissions mismatch")
    if read_xattrs(path) != record.get("xattrs", {}):
        _fail("ATTRIBUTES", "extended attribute mismatch")


def _extract_archive(archive: Path, destination: Path, constraints: dict) -> dict:
    _scan_tar(archive)
    with tarfile.open(archive, mode="r:", encoding="utf-8", errors="strict") as tar:
        first = tar.next()
        if (first is None or first.name != "meta.json" or not first.isreg()
                or not 1 <= first.size <= MAX_METADATA or first.pax_headers):
            _fail("METADATA", "first tar member must be bounded plain meta.json")
        with tar.extractfile(first) as source:
            meta = _json(source.read(MAX_METADATA + 1), MAX_METADATA)
        validate_metadata(meta, **constraints)
        seen = set()
        manifest = meta["manifest"]
        links, directories = [], []
        for info in tar:
            if info is first:
                continue
            name = _path(info.name)
            _host_path(name)
            if name in seen or len(seen) >= MAX_MEMBERS:
                _fail("ARCHIVE", "duplicate/too many members")
            seen.add(name)
            if set(info.pax_headers) - {"path", "linkpath"} or info.sparse is not None:
                _fail("ARCHIVE", "unsupported tar extensions")
            actual = _record(info)
            expected = manifest.get(name)
            if expected is None or actual != {k: v for k, v in expected.items()
                                              if k not in ("sha256", "xattrs")}:
                _fail("MANIFEST", "archive member differs from signed manifest")
            target = destination.joinpath(*name.split("/"))
            # All directories are made ourselves; symlinks are published last.
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if actual["type"] == "directory":
                target.mkdir(exist_ok=True, mode=0o700)
                directories.append((target, expected))
            elif actual["type"] == "symlink":
                links.append((target, expected))
            else:
                digest = hashlib.sha256()
                with tar.extractfile(info) as source, target.open("xb") as output:
                    remaining = info.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            _fail("ARCHIVE", "truncated member")
                        digest.update(chunk)
                        output.write(chunk)
                        remaining -= len(chunk)
                if digest.hexdigest() != expected["sha256"]:
                    _fail("MANIFEST", "file hash mismatch")
                _set_attributes(target, expected)
        if seen != set(manifest):
            _fail("MANIFEST", "missing signed members")
        for target, record in links:
            os.symlink(record["target"], target)
            _set_attributes(target, record)
        for target, record in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
            _set_attributes(target, record)
    return meta


def _private_directory(destination: Path):
    destination = destination.absolute()
    for parent in (destination, *destination.parents):
        if parent.is_symlink():
            _fail("STAGING", "staging path contains a symlink")
    if not destination.exists():
        destination.mkdir(mode=0o700)
    info = destination.lstat()
    if not stat.S_ISDIR(info.st_mode) or destination.is_mount() or any(destination.iterdir()):
        _fail("STAGING", "destination must be an empty private staging directory")
    if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        _fail("STAGING", "staging directory must be owned by caller and mode 0700")
    return destination


def verify_bundle(bundle: Path, signature: Path, keys_dir: Path, destination: Path, *,
                  platform: str, channel: str, current_version: str, highest_version: str,
                  minimum_key_epoch: int, data_schema: int) -> dict:
    """Verify both signatures before decompression, then extract a private tree.

    On failure destination may contain partial, UNTRUSTED data; the caller must
    discard the whole staging directory. No partition or boot pointer is touched.
    """
    bundle, signature, keys_dir = Path(bundle), Path(signature), Path(keys_dir)
    if not _integer(minimum_key_epoch, 1):
        _fail("KEY_EPOCH", "invalid trusted epoch floor")
    destination = _private_directory(Path(destination))
    catalog = _trust(bundle, signature, keys_dir, minimum_key_epoch)
    if shutil.disk_usage(destination).free < catalog["required_staging_bytes"]:
        _fail("SPACE", "insufficient private staging space")
    archive = destination / ".verified-archive.tar"
    constraints = dict(platform=platform, channel=channel, current_version=current_version,
                       highest_version=highest_version, minimum_key_epoch=minimum_key_epoch,
                       data_schema=data_schema)
    try:
        _decompress(bundle, archive, maximum=catalog["uncompressed_size"])
        if archive.stat().st_size != catalog["uncompressed_size"]:
            _fail("LIMIT", "uncompressed size mismatch")
        meta = _extract_archive(archive, destination, constraints)
        for key in ("version", "source_commit", "platform", "channel", "key_epoch"):
            if meta[key] != catalog[key]:
                _fail("CATALOG", "inner and outer identity mismatch")
    except (OSError, tarfile.TarError, UnicodeError) as exc:
        _fail("ARCHIVE", f"cannot extract artifact: {exc}")
    finally:
        archive.unlink(missing_ok=True)
    verify_tree(destination, meta)
    return meta


def verify_tree(destination: Path, metadata: dict) -> None:
    """Rehash a staging tree without following links; no exemptions are skipped."""
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_dir():
        _fail("STAGING", "invalid staging tree")
    manifest = metadata.get("manifest")
    if not isinstance(manifest, dict) or hashlib.sha256(canonical_json(manifest)).hexdigest() != metadata.get("manifest_sha256"):
        _fail("MANIFEST", "invalid verification manifest")
    seen = set()
    pending = [destination]
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            name = path.relative_to(destination).as_posix()
            _path(name)
            info = path.lstat()
            record = manifest.get(name)
            if record is None:
                _fail("MANIFEST", "unexpected staging member")
            seen.add(name)
            kind = record["type"]
            if kind == "symlink":
                if not stat.S_ISLNK(info.st_mode) or os.readlink(path) != record["target"]:
                    _fail("MANIFEST", "symlink mismatch")
            elif kind == "directory":
                if not stat.S_ISDIR(info.st_mode):
                    _fail("MANIFEST", "directory mismatch")
                pending.append(path)
            elif kind == "file":
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size != record["size"] or sha256_file(path) != record["sha256"]):
                    _fail("MANIFEST", "file mismatch")
            else:
                _fail("MANIFEST", "unsupported staging member")
            verify_attributes(path, record)
    if seen != set(manifest):
        _fail("MANIFEST", "missing staging member")
