#!/bin/bash
set -euo pipefail
inputs=/opt/cloudplay-build-inputs
test -f "$inputs/ota-config.json"
apt-get install --no-install-recommends -y minisign zstd rsync gdisk cloud-guest-utils \
    dosfstools e2fsprogs util-linux
install -d /usr/local/lib/cloudplay/updater /usr/lib/cloudplay/image \
    /usr/share/cloudplay/update-keys /etc/systemd/system.conf.d /data
install -m 644 "$inputs"/updater/*.py /usr/local/lib/cloudplay/updater/
install -m 644 "$inputs"/image-build/*.py "$inputs"/image-build/autoboot.txt /usr/lib/cloudplay/image/
install -m 644 "$inputs"/image-build/*.service "$inputs"/image-build/*.timer /etc/systemd/system/
install -m 644 "$inputs/image-build/cloudplay-updater-greetd.conf" \
    /etc/systemd/system/greetd.service.d/cloudplay-updater.conf
install -m 644 "$inputs"/image-build/keys/*.pub /usr/share/cloudplay/update-keys/
install -m 644 "$inputs/ota-config.json" /etc/cloudplay/updater.json
install -m 644 "$inputs/ota-release.json" /usr/share/cloudplay/release.json
install -m 644 /boot/firmware/cmdline.txt /usr/share/cloudplay/kernel-command-line.txt
printf '1\n' > /etc/cloudplay/ota-enabled
printf '[Manager]\nRuntimeWatchdogSec=30s\nRebootWatchdogSec=2min\n' \
    > /etc/systemd/system.conf.d/cloudplay-watchdog.conf
printf '\n[all]\ndtparam=watchdog=on\nkernel_watchdog_timeout=300\n' \
    >> /boot/firmware/config.txt
systemctl mask apt-daily.service apt-daily.timer apt-daily-upgrade.service \
    apt-daily-upgrade.timer unattended-upgrades.service
systemctl enable cloudplay-data.service cloudplay-update-bootstrap.service cloudplay-updater.service \
    cloudplay-update-shutdown.service cloudplay-update-early-guard.service \
    cloudplay-update-health.timer cloudplay-update-deadline.timer
PYTHONPATH=/usr/local/lib/cloudplay python3 -B - <<'PY'
from updater.state import Config
value = Config.load("/etc/cloudplay/updater.json")
assert not value.experimental_hardware_validation
assert not value.launcher_isolation_verified
assert value.launcher_uid == value.browser_uid == 1000
PY
