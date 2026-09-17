#!/bin/bash
set -euo pipefail
on_chroot <<'CHROOT'
# pi-gen suppresses -u during assembly; enable it before checking the new theme.
sed -i 's/^update_initramfs=.*/update_initramfs=yes/' /etc/initramfs-tools/update-initramfs.conf
grep -q '^update_initramfs=yes$' /etc/initramfs-tools/update-initramfs.conf
update-initramfs -u -k all
CHROOT
# pi-gen bind-mounts /dev without its /dev/shm submount. wlroots needs POSIX shm.
if ! mountpoint -q "${ROOTFS_DIR}/dev/shm"; then
    mount --bind /dev/shm "${ROOTFS_DIR}/dev/shm"
fi
on_chroot <<'CHROOT'
python3 -B -c 'import sys; sys.path.insert(0, "/usr/local/lib/cloudplay/onboarding"); import network, service, readiness, boot, dbus, qrcode.image.svg'
systemd-analyze verify --man=no /etc/systemd/system/cloudplay-network.service \
    /etc/systemd/system/cloudplay-wifi-radio.service \
    /etc/systemd/system/cloudplay-startup.service greetd.service plymouth-quit.service
runuser -u cloudplay -- test -w /dev/shm
runuser -u cloudplay -- python3 -B - <<'PY'
import os
from pathlib import Path
for directory in (Path("/home/cloudplay"), Path("/home/cloudplay/.config"),
                  Path("/home/cloudplay/.config/cloudplay")):
    probe = directory / ".cloudplay-build-write-check"
    fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    probe.unlink()
PY
install -d -m 700 -o cloudplay -g cloudplay /run/cloudplay-config-check
runuser -u cloudplay -- env XDG_RUNTIME_DIR=/run/cloudplay-config-check \
    WLR_BACKENDS=headless WLR_RENDERER=pixman \
    timeout --kill-after=5 20 dbus-run-session -- labwc -C /etc/cloudplay/labwc -S '/bin/sleep 1'
rm -rf /run/cloudplay-config-check
python3 /opt/cloudplay-build-inputs/verify-kiosk.py
python3 /opt/cloudplay-build-inputs/package-manifest.py
CHROOT
mkdir -p "${DEPLOY_DIR}/provenance"
cp "${ROOTFS_DIR}/usr/local/share/cloudplay/"{build-manifest.json,packages.tsv} "${DEPLOY_DIR}/provenance/"
cp "${ROOTFS_DIR}/usr/local/share/cloudplay/kiosk-verification.json" "${DEPLOY_DIR}/provenance/"
cp "${ROOTFS_DIR}/opt/cloudplay-build-inputs/"{manifest.json,cloudplay-commit.txt,cloudplay-working-tree.patch,builder-os-release,builder-packages.tsv} "${DEPLOY_DIR}/provenance/"
