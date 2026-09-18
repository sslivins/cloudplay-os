#!/bin/bash
set -euo pipefail
[[ "${CLOUDPLAY_OTA_EXPERIMENTAL:-0}" == 1 ]]
: "${ROOTFS_DIR:?}" "${DEPLOY_DIR:?}"
destination="${DEPLOY_DIR}/ota-inputs"
[[ ! -e "$destination" ]]
install -d "$destination/root" "$destination/boot"
# Never archive live pseudo-filesystems or the separately mounted FAT filesystem.
rsync -aHAXx --numeric-ids \
    --exclude='/dev/*' --exclude='/proc/*' --exclude='/sys/*' --exclude='/run/*' \
    --exclude='/boot/firmware/*' --exclude='/data/*' \
    "${ROOTFS_DIR}/" "$destination/root/"
rsync -rt "${ROOTFS_DIR}/boot/firmware/" "$destination/boot/"
find "$destination/boot" -type d -exec chmod 755 {} +
find "$destination/boot" -type f -exec chmod 644 {} +
chown -R 0:0 "$destination/boot"
# Slot mirrors are generated from local boot state, not signed immutable payload.
cp "${ROOTFS_DIR}/usr/lib/cloudplay/image/autoboot.txt" "$destination/boot/autoboot.txt"
test ! -e "$destination/boot/tryboot.txt"
