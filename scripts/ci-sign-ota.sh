#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "${GITHUB_ACTIONS:-}" == true && "${GITHUB_REF:-}" == refs/heads/main ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ && "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]]
: "${RUNNER_TEMP:?Missing isolated runner temporary directory}"
[[ -n "${CLOUDPLAY_OTA_SIGNING_KEY:-}" ]] || { echo "Missing protected primary signing secret" >&2; exit 1; }
directory="$RUNNER_TEMP/cloudplay-ota-signing-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
mkdir -m 700 -- "$directory"
cleanup() {
    rm -f -- "$directory/primary.key" "$directory/probe" "$directory/probe.minisig"
    rmdir -- "$directory"
}
trap cleanup EXIT
export CLOUDPLAY_OTA_SECRET_KEY="$directory/primary.key"
python3 - <<'PY'
import os
from pathlib import Path
key = os.environ["CLOUDPLAY_OTA_SIGNING_KEY"].encode("ascii")
if not 20 <= len(key) <= 8192:
    raise SystemExit("Invalid signing-secret length")
fd = os.open(os.environ["CLOUDPLAY_OTA_SECRET_KEY"],
             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "wb") as output:
    output.write(key)
    if not key.endswith(b"\n"):
        output.write(b"\n")
PY
unset CLOUDPLAY_OTA_SIGNING_KEY
python3 image-build/preflight.py
printf 'Cloudplay OTA signing-key correspondence check\n' > "$directory/probe"
minisign -Sm "$directory/probe" -s "$CLOUDPLAY_OTA_SECRET_KEY" \
    -x "$directory/probe.minisig" </dev/null
minisign -Vm "$directory/probe" -x "$directory/probe.minisig" \
    -p "image-build/keys/epoch-$CLOUDPLAY_OTA_KEY_EPOCH-primary.pub" </dev/null
# Signing starts only after pi-gen exits. Never forward the raw secret or GH token.
sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    CLOUDPLAY_OTA_EXPERIMENTAL=1 \
    CLOUDPLAY_OTA_VERSION="$CLOUDPLAY_OTA_VERSION" \
    CLOUDPLAY_OTA_MINIMUM_VERSION="$CLOUDPLAY_OTA_MINIMUM_VERSION" \
    CLOUDPLAY_OTA_PLATFORM="$CLOUDPLAY_OTA_PLATFORM" \
    CLOUDPLAY_OTA_KEY_EPOCH="$CLOUDPLAY_OTA_KEY_EPOCH" \
    CLOUDPLAY_OTA_WORKFLOW="$CLOUDPLAY_OTA_WORKFLOW" \
    CLOUDPLAY_OTA_SECRET_KEY="$CLOUDPLAY_OTA_SECRET_KEY" \
    python3 image-build/assemble.py
