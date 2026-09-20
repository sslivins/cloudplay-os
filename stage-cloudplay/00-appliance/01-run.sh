#!/bin/bash
set -euo pipefail
inputs="${BASE_DIR}/cloudplay-inputs"
install -d "${ROOTFS_DIR}/usr/local/lib/cloudplay" "${ROOTFS_DIR}/usr/local/bin" \
    "${ROOTFS_DIR}/usr/local/share/cloudplay" "${ROOTFS_DIR}/opt/cloudplay-build-inputs" \
    "${ROOTFS_DIR}/etc/cloudplay/labwc" "${ROOTFS_DIR}/etc/greetd" \
    "${ROOTFS_DIR}/etc/systemd/system/greetd.service.d" \
    "${ROOTFS_DIR}/etc/systemd/system/plymouth-quit.service.d" \
    "${ROOTFS_DIR}/etc/systemd/logind.conf.d" \
    "${ROOTFS_DIR}/etc/systemd/journald.conf.d" \
    "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d" \
    "${ROOTFS_DIR}/etc/udev/rules.d" \
    "${ROOTFS_DIR}/etc/chromium/policies/managed" \
    "${ROOTFS_DIR}/usr/share/plymouth/themes/cloudplay"
cp -a "${inputs}/." "${ROOTFS_DIR}/opt/cloudplay-build-inputs/"
cp -a "${inputs}/onboarding" "${ROOTFS_DIR}/usr/local/lib/cloudplay/"
cp -a "${inputs}/launcher" "${ROOTFS_DIR}/usr/local/lib/cloudplay/"
find "${ROOTFS_DIR}/usr/local/lib/cloudplay/launcher" -type d -exec chmod 755 {} +
find "${ROOTFS_DIR}/usr/local/lib/cloudplay/launcher" -type f -exec chmod 644 {} +
install -m 644 files/71-cloudplay-gamepad.rules "${ROOTFS_DIR}/etc/udev/rules.d/"
find "${ROOTFS_DIR}/usr/local/lib/cloudplay/onboarding" -type d -exec chmod 755 {} +
find "${ROOTFS_DIR}/usr/local/lib/cloudplay/onboarding" -type f -exec chmod 644 {} +
install -m 644 files/cloudplay-network.service files/cloudplay-wifi-radio.service \
    files/cloudplay-startup.service "${ROOTFS_DIR}/etc/systemd/system/"
install -m 644 files/cloudplay-diagnostics.conf "${ROOTFS_DIR}/etc/systemd/journald.conf.d/cloudplay-diagnostics.conf"
install -m 755 files/cloudplay-start files/cloudplay-home files/cloudplay-session files/cloudplay-browser-session "${ROOTFS_DIR}/usr/local/bin/"
install -m 755 files/cloudplay-development-ssh "${ROOTFS_DIR}/usr/local/bin/"
case "${CLOUDPLAY_DEVELOPMENT_SSH:-1}" in 0|1) ;; *) exit 1 ;; esac
printf '%s\n' "${CLOUDPLAY_DEVELOPMENT_SSH:-1}" > "${ROOTFS_DIR}/etc/cloudplay/development-ssh"
install -m 755 "${inputs}/install-extension.py" "${inputs}/supervise.py" "${ROOTFS_DIR}/usr/local/lib/cloudplay/"
install -m 755 "${inputs}/hdr-readiness.py" "${ROOTFS_DIR}/usr/local/bin/cloudplay-hdr-check"
install -m 644 files/SECURITY.txt "${ROOTFS_DIR}/usr/local/share/cloudplay/"
install -m 644 files/cloudplay-browser-policy.json "${ROOTFS_DIR}/etc/chromium/policies/managed/cloudplay.json"
install -m 644 files/greetd.toml "${ROOTFS_DIR}/etc/greetd/config.toml"
install -m 644 files/greetd-kiosk.conf "${ROOTFS_DIR}/etc/systemd/system/greetd.service.d/cloudplay.conf"
install -m 644 files/labwc-rc.xml "${ROOTFS_DIR}/etc/cloudplay/labwc/rc.xml"
install -m 644 files/labwc-menu.xml "${ROOTFS_DIR}/etc/cloudplay/labwc/menu.xml"
install -m 755 files/labwc-autostart "${ROOTFS_DIR}/etc/cloudplay/labwc/autostart"
install -m 644 files/cloudplay.plymouth files/cloudplay.script files/cloudplay.png \
    files/cloudplay-background.png files/spinner-*.png "${ROOTFS_DIR}/usr/share/plymouth/themes/cloudplay/"
install -m 644 files/plymouth-quit.conf "${ROOTFS_DIR}/etc/systemd/system/plymouth-quit.service.d/cloudplay.conf"
install -m 644 files/cloud-init-kiosk.cfg "${ROOTFS_DIR}/etc/cloud/cloud.cfg.d/zz-cloudplay.cfg"
install -m 644 files/cloud-init-kiosk.cfg "${ROOTFS_DIR}/boot/firmware/user-data"
install -m 644 files/network-config "${ROOTFS_DIR}/boot/firmware/network-config"
printf '[Login]\nNAutoVTs=0\nReserveVT=0\n' > "${ROOTFS_DIR}/etc/systemd/logind.conf.d/cloudplay.conf"
on_chroot <<'CHROOT'
set -eu
apt-get purge -y userconf-pi rpi-connect-lite
apt-get install --no-install-recommends --allow-downgrades -y /opt/cloudplay-build-inputs/artifacts/*.deb
apt-mark hold chromium chromium-common chromium-sandbox chromium-l10n
groupadd --system --force cloudplay-gamepad
usermod --password '*' --shell /bin/bash --groups audio,video,render,cloudplay-gamepad cloudplay
usermod --password '*' root
install -d -m 700 -o cloudplay -g cloudplay /home/cloudplay \
    /home/cloudplay/.config /home/cloudplay/.config/cloudplay
install -d -m 2755 -o root -g systemd-journal /var/log/journal
systemctl mask ssh.service ssh.socket userconfig.service getty@.service serial-getty@.service
/usr/local/bin/cloudplay-development-ssh "$(cat /etc/cloudplay/development-ssh)"
# No build-host SSH identity may be shared by flashed devices.
rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
systemctl enable regenerate_ssh_host_keys.service
systemctl enable greetd.service
systemctl enable cloudplay-network.service cloudplay-wifi-radio.service cloudplay-startup.service
systemctl set-default graphical.target
plymouth-set-default-theme cloudplay
printf '[Daemon]\nTheme=cloudplay\nShowDelay=0\nDeviceTimeout=10\n' > /etc/plymouth/plymouthd.conf
printf '\nvc4\nv3d\n' >> /etc/initramfs-tools/modules
python3 - <<'PY'
import json, subprocess
from pathlib import Path
cmdline = Path("/boot/firmware/cmdline.txt")
args = [arg for arg in cmdline.read_text().split() if not arg.startswith(("console=", "loglevel="))]
args += ["console=tty3", "quiet", "splash", "loglevel=3", "rd.udev.log_level=3",
         "vt.global_cursor_default=0", "logo.nologo", "systemd.show_status=false",
         "plymouth.ignore-serial-consoles"]
cmdline.write_text(" ".join(dict.fromkeys(args)) + "\n")
with Path("/boot/firmware/config.txt").open("a") as output:
    output.write("\n[all]\ndisable_splash=1\n")
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
CHROOT
if [[ -f "${inputs}/ota-config.json" ]]; then
    on_chroot <<'CHROOT'
set -eu
bash /opt/cloudplay-build-inputs/install-ota.sh
CHROOT
fi
