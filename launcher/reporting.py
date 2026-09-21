"""Small, local-only diagnostic preview and GitHub issue QR payload."""
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlencode

from updates import BUSY

ISSUES = "https://github.com/sslivins/cloudplay-os/issues/new"
MAX_URL_BYTES = 240
PHASES = BUSY | {"idle", "available", "failed", "rolled_back", "promoted",
                 "ready_to_restart", "recovery_required", "uninitialized", "unavailable"}
ERROR_CODES = frozenset({
    "NETWORK", "HTTP", "BACKOFF", "RATE_LIMIT", "SPACE", "SIGNATURE", "RELEASE_CHANGED",
    "BUSY", "CANCEL_TOO_LATE", "CANCELLED", "STRIKE_LIMIT", "HARDWARE_GATE",
    "ISOLATION_GATE", "UNINITIALIZED", "INTERRUPTED", "STAGING", "PROFILE", "IO",
    "INTERNAL", "HEALTH_NOT_READY", "STAGING_DEADLINE", "ROLLBACK_LOOP",
    "VERSION", "SLOT", "RECOVERY", "MANIFEST", "ARCHIVE", "IPC", "STATE",
})


def build_report(version, status, free_bytes):
    if not isinstance(version, str) or not re.fullmatch(
            r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-beta\.[0-9]{1,4})?", version):
        raise ValueError("Installed OS version is unavailable or unsupported")
    if not isinstance(status, dict):
        raise ValueError("Update status is unavailable")
    phase = status.get("phase")
    phase = phase if isinstance(phase, str) and phase in PHASES else "unknown"
    error = status.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    code = code if isinstance(code, str) and code in ERROR_CODES else "OTHER" if error else "none"
    if type(free_bytes) is not int or not 0 <= free_bytes <= 99999 * 1024**3:
        raise ValueError("Free storage measurement is unavailable")
    free_gib = (free_bytes + 1024**3 // 2) // 1024**3
    summary = f"OS: {version}\nUpdate: {phase}\nError: {code}\nFree: ~{free_gib} GiB"
    body = "What happened?\n\n\n" + summary
    url = ISSUES + "?" + urlencode({"title": "Problem report", "body": body})
    if len(url.encode("ascii")) > MAX_URL_BYTES:
        raise ValueError("Report is too large for the compact QR code")
    return summary, url


def collect_report(status, *, release_file=Path("/usr/share/cloudplay/release.json"),
                   data_path="/data"):
    with release_file.open(encoding="utf-8") as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("Installed release metadata exceeds its size limit")
    release = json.loads(raw)
    if not isinstance(release, dict):
        raise ValueError("Installed release metadata is invalid")
    return build_report(release.get("version"), status, shutil.disk_usage(data_path).free)


def qr_pixels(url, module_pixels):
    import qrcode
    if not url.startswith(ISSUES + "?") or len(url.encode("ascii")) > MAX_URL_BYTES:
        raise ValueError("Invalid report QR payload")
    if type(module_pixels) is not int or not 3 <= module_pixels <= 8:
        raise ValueError("Invalid QR display size")
    qr = qrcode.QRCode(version=11, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=4)
    qr.add_data(url, optimize=0)
    qr.make(fit=False)
    matrix = qr.get_matrix()
    rows = []
    for row in matrix:
        pixels = b"".join((b"\x00\x00\x00" if cell else b"\xff\xff\xff") * module_pixels
                          for cell in row)
        rows.append(pixels * module_pixels)
    return len(matrix) * module_pixels, b"".join(rows)
