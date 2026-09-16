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
python3 scripts/artifacts.py fetch
commit="$(python3 -c 'import json; print(json.load(open("manifest.json"))["pi_gen"]["commit"])')"
git init -q build/pi-gen
git -C build/pi-gen remote add origin https://github.com/RPi-Distro/pi-gen.git
git -C build/pi-gen fetch --depth=1 origin "$commit"
git -C build/pi-gen checkout --detach FETCH_HEAD
[[ "$(git -C build/pi-gen rev-parse HEAD)" == "$commit" ]]
cp config build/pi-gen/config
cp -a stage-cloudplay build/pi-gen/
mkdir -p build/pi-gen/cloudplay-inputs build/pi-gen/export-image/04-cloudplay-manifest
cp manifest.json scripts/install-extension.py scripts/package-manifest.py scripts/hdr-readiness.py build/pi-gen/cloudplay-inputs/
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
