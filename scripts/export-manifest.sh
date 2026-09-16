#!/bin/bash
set -euo pipefail
on_chroot <<'CHROOT'
update-initramfs -u -k all
python3 /opt/cloudplay-build-inputs/verify-kiosk.py
python3 /opt/cloudplay-build-inputs/package-manifest.py
CHROOT
mkdir -p "${DEPLOY_DIR}/provenance"
cp "${ROOTFS_DIR}/usr/local/share/cloudplay/"{build-manifest.json,packages.tsv} "${DEPLOY_DIR}/provenance/"
cp "${ROOTFS_DIR}/usr/local/share/cloudplay/kiosk-verification.json" "${DEPLOY_DIR}/provenance/"
cp "${ROOTFS_DIR}/opt/cloudplay-build-inputs/"{manifest.json,cloudplay-commit.txt,cloudplay-working-tree.patch,builder-os-release,builder-packages.tsv} "${DEPLOY_DIR}/provenance/"
