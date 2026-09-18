#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/artifacts.py validate
[[ "$(uname -m)" == aarch64 ]] || { echo "Native ARM64 Linux required" >&2; exit 1; }
[[ $EUID == 0 ]] || { echo "Run with sudo on a dedicated ARM64 builder" >&2; exit 1; }
[[ "$PWD" != *" "* ]] || { echo "No spaces allowed in build path" >&2; exit 1; }
status="$(git -c safe.directory="$PWD" status --porcelain)"
[[ -z "$status" ]] || { echo "Commit reviewed source before building" >&2; exit 1; }
[[ ! -e build/pi-gen ]] || { echo "Use a clean checkout/build directory, no resumed image builds" >&2; exit 1; }
case "${CLOUDPLAY_OTA_EXPERIMENTAL:-0}:${CLOUDPLAY_OTA_DEFER_SIGNING:-0}" in
    0:0) ;;
    1:0) python3 image-build/preflight.py ;;
    1:1) python3 image-build/preflight.py --payload-only ;;
    *) echo "Invalid experimental/deferred-signing build mode" >&2; exit 1 ;;
esac
python3 scripts/artifacts.py fetch
commit="$(python3 -c 'import json; print(json.load(open("manifest.json"))["pi_gen"]["commit"])')"
git init -q build/pi-gen
git -C build/pi-gen remote add origin https://github.com/RPi-Distro/pi-gen.git
git -C build/pi-gen fetch --depth=1 origin "$commit"
git -C build/pi-gen checkout --detach FETCH_HEAD
[[ "$(git -C build/pi-gen rev-parse HEAD)" == "$commit" ]]
cp config build/pi-gen/config
if [[ "${CLOUDPLAY_OTA_EXPERIMENTAL:-0}" == 1 ]]; then
    printf '\nexport CLOUDPLAY_OTA_EXPERIMENTAL=1\n' >> build/pi-gen/config
fi
cp -a stage-cloudplay build/pi-gen/
# Lite otherwise installs userconf-pi again and enables the OS account wizard.
rm -rf build/pi-gen/export-image/01-user-rename
mkdir -p build/pi-gen/cloudplay-inputs build/pi-gen/export-image/04-cloudplay-manifest
mkdir -p build/pi-gen/cloudplay-inputs/onboarding
mkdir -p build/pi-gen/cloudplay-inputs/launcher
cp launcher/{main.py,host.py,gamepad.py,updates.py,maintenance.py,heartbeat.py} build/pi-gen/cloudplay-inputs/launcher/
cp -r launcher/assets build/pi-gen/cloudplay-inputs/launcher/
cp onboarding/{network.py,service.py,client.py,readiness.py,boot.py,setup.html,setup.js,setup.css} \
    build/pi-gen/cloudplay-inputs/onboarding/
cp manifest.json scripts/install-extension.py scripts/package-manifest.py scripts/hdr-readiness.py \
    scripts/supervise.py scripts/verify-kiosk.py scripts/check-launcher.py build/pi-gen/cloudplay-inputs/
if [[ "${CLOUDPLAY_OTA_EXPERIMENTAL:-0}" == 1 ]]; then
    mkdir -p build/pi-gen/cloudplay-inputs/{updater,image-build/keys}
    cp updater/*.py build/pi-gen/cloudplay-inputs/updater/
    cp image-build/{firstboot.py,layout.py,boot-service.py,recovery.py,autoboot.txt,snapshot.sh} \
        build/pi-gen/cloudplay-inputs/image-build/
    cp image-build/*.service image-build/*.timer build/pi-gen/cloudplay-inputs/image-build/
    cp image-build/cloudplay-updater-greetd.conf build/pi-gen/cloudplay-inputs/image-build/
    cp image-build/update-labwc-rc.xml build/pi-gen/cloudplay-inputs/image-build/
    cp image-build/keys/*.pub build/pi-gen/cloudplay-inputs/image-build/keys/
    cp image-build/hardware-approval.json build/pi-gen/cloudplay-inputs/image-build/
    cp scripts/install-ota.sh build/pi-gen/cloudplay-inputs/
    python3 image-build/configure.py --output build/pi-gen/cloudplay-inputs/ota-config.json
fi
python3 scripts/artifacts.py stage --destination build/pi-gen/cloudplay-inputs/artifacts
cp scripts/export-manifest.sh build/pi-gen/export-image/04-cloudplay-manifest/00-run.sh
git -c safe.directory="$PWD" rev-parse HEAD > build/pi-gen/cloudplay-inputs/cloudplay-commit.txt
git -c safe.directory="$PWD" diff --binary HEAD > build/pi-gen/cloudplay-inputs/cloudplay-working-tree.patch
cp /etc/os-release build/pi-gen/cloudplay-inputs/builder-os-release
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' > build/pi-gen/cloudplay-inputs/builder-packages.tsv
find build/pi-gen/stage-cloudplay -name '*.sh' -exec chmod 755 {} +
chmod 755 build/pi-gen/export-image/04-cloudplay-manifest/00-run.sh
touch build/pi-gen/stage2/SKIP_IMAGES
cd build/pi-gen
./build.sh
cd deploy
sha256sum ./*.img.xz > SHA256SUMS
if [[ "${CLOUDPLAY_OTA_EXPERIMENTAL:-0}" == 1 ]]; then
    cd ../../..
    if [[ "${CLOUDPLAY_OTA_DEFER_SIGNING:-0}" == 1 ]]; then
        test -d build/pi-gen/deploy/ota-inputs/root
        test -d build/pi-gen/deploy/ota-inputs/boot
        echo "Unsigned OTA payload ready; signing and image assembly are still required."
    else
        python3 image-build/assemble.py
    fi
fi
