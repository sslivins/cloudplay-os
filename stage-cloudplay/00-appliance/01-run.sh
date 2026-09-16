#!/bin/bash
set -euo pipefail
inputs="${BASE_DIR}/cloudplay-inputs"
install -d "${ROOTFS_DIR}/usr/local/lib/cloudplay" "${ROOTFS_DIR}/usr/local/bin" \
    "${ROOTFS_DIR}/usr/local/share/cloudplay" "${ROOTFS_DIR}/usr/share/applications" \
    "${ROOTFS_DIR}/etc/xdg/autostart" "${ROOTFS_DIR}/opt/cloudplay-build-inputs"
cp -a "${inputs}/." "${ROOTFS_DIR}/opt/cloudplay-build-inputs/"
install -m 755 files/cloudplay-start files/cloudplay-settings "${ROOTFS_DIR}/usr/local/bin/"
install -m 755 "${inputs}/install-extension.py" "${ROOTFS_DIR}/usr/local/lib/cloudplay/"
install -m 755 "${inputs}/hdr-readiness.py" "${ROOTFS_DIR}/usr/local/bin/cloudplay-hdr-check"
install -m 644 files/cloudplay.desktop files/cloudplay-settings.desktop "${ROOTFS_DIR}/usr/share/applications/"
install -m 644 files/cloudplay-autostart.desktop "${ROOTFS_DIR}/etc/xdg/autostart/"
install -m 644 files/SECURITY.txt "${ROOTFS_DIR}/usr/local/share/cloudplay/"
on_chroot <<'CHROOT'
set -eu
apt-get install --allow-downgrades -y /opt/cloudplay-build-inputs/artifacts/*.deb
apt-mark hold chromium chromium-common chromium-sandbox chromium-l10n
python3 - <<'PY'
import json, subprocess
from pathlib import Path
lock = json.loads(Path("/opt/cloudplay-build-inputs/manifest.json").read_text())
for package in ("chromium", "chromium-common", "chromium-sandbox", "chromium-l10n"):
    actual = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", package], text=True)
    if actual != lock["browser"]["package_version"]:
        raise SystemExit("Browser package mismatch: " + package)
ext = lock["extension"]
subprocess.run([
    "python3", "/usr/local/lib/cloudplay/install-extension.py",
    "/opt/cloudplay-build-inputs/artifacts/extension.tar.gz", ext["commit"], ext["sha256"],
], check=True)
PY
rm -rf /opt/cloudplay-build-inputs/artifacts
systemctl disable ssh.service
CHROOT
# B4 is set before the stock export user-rename hook, which installs the wizard.
on_chroot <<CHROOT
SUDO_USER="${FIRST_USER_NAME}" raspi-config nonint do_wayland W2
SUDO_USER="${FIRST_USER_NAME}" raspi-config nonint do_boot_behaviour B4
CHROOT
