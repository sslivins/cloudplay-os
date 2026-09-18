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
python3 -B -c 'import sys; sys.path.insert(0, "/usr/local/lib/cloudplay/launcher"); import host, gamepad, main, gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
install -d -m 755 /run/sshd
systemd-analyze verify --generators=yes --man=no /etc/systemd/system/cloudplay-network.service \
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
rm -f /run/cloudplay-config-check/home-smoke.json
runuser -u cloudplay -- env XDG_RUNTIME_DIR=/run/cloudplay-config-check \
    CLOUDPLAY_SMOKE_RESULT=/run/cloudplay-config-check/home-smoke.json \
    WLR_BACKENDS=headless WLR_RENDERER=pixman \
    timeout --kill-after=5 20 dbus-run-session -- labwc -C /etc/cloudplay/labwc \
    -S '/usr/bin/python3 /opt/cloudplay-build-inputs/check-launcher.py'
# labwc need not propagate its session client's failure; require explicit success.
test -f /run/cloudplay-config-check/home-smoke.json
install -m 644 /run/cloudplay-config-check/home-smoke.json /usr/local/share/cloudplay/home-smoke.json
if [[ -f /etc/cloudplay/ota-enabled ]]; then
    runuser -u cloudplay -- env XDG_RUNTIME_DIR=/run/cloudplay-config-check \
        CLOUDPLAY_SMOKE_UPDATES=1 \
        CLOUDPLAY_SMOKE_RESULT=/run/cloudplay-config-check/updates-smoke.json \
        WLR_BACKENDS=headless WLR_RENDERER=pixman \
        timeout --kill-after=5 20 dbus-run-session -- labwc -C /etc/cloudplay/labwc \
        -S '/usr/bin/python3 /opt/cloudplay-build-inputs/check-launcher.py'
    test -f /run/cloudplay-config-check/updates-smoke.json
    install -m 644 /run/cloudplay-config-check/updates-smoke.json /usr/local/share/cloudplay/updates-smoke.json
    install -d -m 700 -o cloudplay-update -g cloudplay-update /run/cloudplay-trusted-check
    runuser -u cloudplay-update -- env XDG_RUNTIME_DIR=/run/cloudplay-trusted-check \
        CLOUDPLAY_SMOKE_TRUSTED=1 \
        CLOUDPLAY_SMOKE_RESULT=/run/cloudplay-trusted-check/trusted-smoke.json \
        WLR_BACKENDS=headless WLR_RENDERER=pixman \
        timeout --kill-after=5 20 dbus-run-session -- labwc -C /etc/cloudplay/update-labwc \
        -S '/usr/bin/python3 /opt/cloudplay-build-inputs/check-launcher.py'
    test -f /run/cloudplay-trusted-check/trusted-smoke.json
    install -m 644 /run/cloudplay-trusted-check/trusted-smoke.json /usr/local/share/cloudplay/trusted-smoke.json
    rm -rf /run/cloudplay-trusted-check
    python3 -B - <<'PY'
import json
from pathlib import Path
for name in ("home-smoke.json", "updates-smoke.json", "trusted-smoke.json"):
    assert json.loads((Path("/usr/local/share/cloudplay") / name).read_text()) == {"passed": True}
record = Path("/usr/share/cloudplay/release.json")
release = json.loads(record.read_text())
release["launcher_smoke_passed"] = True
record.write_text(json.dumps(release, sort_keys=True) + "\n")
PY
fi
rm -rf /run/cloudplay-config-check
python3 /opt/cloudplay-build-inputs/verify-kiosk.py
python3 /opt/cloudplay-build-inputs/package-manifest.py
CHROOT
mkdir -p "${DEPLOY_DIR}/provenance"
cp "${ROOTFS_DIR}/usr/local/share/cloudplay/"{build-manifest.json,packages.tsv} "${DEPLOY_DIR}/provenance/"
cp "${ROOTFS_DIR}/usr/local/share/cloudplay/kiosk-verification.json" "${DEPLOY_DIR}/provenance/"
cp "${ROOTFS_DIR}/opt/cloudplay-build-inputs/"{manifest.json,cloudplay-commit.txt,cloudplay-working-tree.patch,builder-os-release,builder-packages.tsv} "${DEPLOY_DIR}/provenance/"
if [[ "${CLOUDPLAY_OTA_EXPERIMENTAL:-0}" == 1 ]]; then
    bash "${ROOTFS_DIR}/opt/cloudplay-build-inputs/image-build/snapshot.sh"
fi
