# Maintenance and trust boundaries

## Browser hold is security debt

`chromium`, `chromium-common`, `chromium-sandbox`, and `chromium-l10n` are held
together to prevent an ordinary apt upgrade replacing the custom HEVC build
or mixing versions. This is **not** a safe indefinitely frozen browser.
Monitor upstream security advisories, rebuild in the separate browser project,
review all four new artifact digests, update this manifest and run acceptance.
There is no scheduled update mechanism or maximum-age enforcement yet.

For a coordinated local experiment only, close Chromium, download and verify
all four reviewed replacements, `sudo apt-mark unhold` those four packages,
install the four together, check `dpkg-query -W` versions, then reapply
`sudo apt-mark hold` and retest. Prefer a new reviewed image for distribution;
in-image build provenance describes the original image, not later local edits.
Do not drop the sandbox to fix decoder, extension or login problems.

## Extension is independently replaceable, not trusted OTA

The installed helper accepts a local archive, full reviewed commit, and SHA256:

```sh
sudo python3 /usr/local/lib/cloudplay/install-extension.py \
  ./extension.tar.gz '<40-character-reviewed-commit>' '<64-character-sha256>'
```

Close all Cloudplay Chromium windows first. The helper hashes the exact bytes
it extracts, rejects links/path traversal/nonregular files/oversized payloads,
requires an MV3 root manifest, and stages files under a root-owned version
directory. It makes files 0444/directories 0555 and atomically replaces the
`/opt/gfn-pi-compat` symlink. These are user-immutable permissions, **not**
filesystem verity, `chattr +i`, or protection from root. The previous version
is retained. Restart Chromium and verify extension version/behavior; a running
browser can retain old extension state across the filesystem switch.

Rollback is an explicit administrator operation with Chromium closed:

```sh
# Choose an existing, previously reviewed /opt/cloudplay/extensions/<commit>-<sha256>.
sudo ln -s '/opt/cloudplay/extensions/<previous-reviewed-directory>' /opt/gfn-pi-compat.rollback
sudo mv -Tf /opt/gfn-pi-compat.rollback /opt/gfn-pi-compat
```

No background downloader, update URL, signature validation or key provision is
present. An independently obtained trusted digest and review are prerequisites;
HTTPS plus a checksum on the same server is not an independent trust anchor.
The prototype `--load-extension` path is not an enterprise extension policy.
Verify this specific Chromium package really loads the MV3 extension and that
GFN's page receives only truthful capabilities before relying on it.

## Image distribution

CI artifacts are unsigned developer previews, not an authenticated OS release.
SHA256SUMS detects accidental changes but cannot prove publisher identity.
Do not create a signed public release until a signing/trust-anchor design,
key custody, verification instructions and recovery/revocation policy exist.
OS updates are manual reflash initially; retain a known-good spare card.
Do not upload browser profiles, Wi-Fi secrets or diagnostic tokens with public
acceptance reports. Auth/session information is sensitive.

## Why a stock desktop

At pinned pi-gen commit `74d08a337bd29da289b9aedbe5b48c79fb2e5a03`,
stage3 installs `rpd-wayland-core`, `rpd-x-core`, `rpd-preferences` and
`rpd-theme`; stage4 adds applications/developer/graphics/utilities/extras.
The README's historical LXDE wording is not sufficient to choose a modern
session. The recipe retains stage3 and explicitly selects labwc via
`raspi-config nonint do_wayland W2`; it does not replace compositor configs.
Current `rpd-wayland-core` dependencies provide panel network/Bluetooth/audio
plugins; its `/etc/xdg/labwc/autostart` runs `lxsession-xdg-autostart`.
This was checked against `rpd-wayland-core` 1.29 and the W2 selector against
`raspi-config-core` 20260730 in the Trixie apt repository. These apt packages
float and must be revalidated at image-build time.

The stock export `01-user-rename/01-run.sh` calls `rename-user -f -s`, creating
the isolated `rpi-first-boot-wizard` session and enabling userconfig. The recipe
sets desktop autologin **before** that export hook and never removes piwiz.
Normal owner launch is gated while the wizard autostart file exists. If a
future userconf package leaves this file behind, investigate onboarding rather
than bypassing the guard with a privileged launch.

Labwc is a practical onboarding/recovery MVP, **not evidence of an HDR-capable
presentation path**. No SDR-only forced color profile, fixed 1080p output,
8-bit override or permanently SDR-specific compositor fork is baked in.
End-to-end HDR may require upgrading labwc/wlroots, kernel, Mesa or Chromium;
switching compositor families is not inherently necessary.

## Labwc HDR prerequisites

Upstream release notes explicitly document **labwc 0.20.0 (2026-05-25)** adding
HDR10 output with the **Vulkan renderer**, introduced by PR #3424. The upstream
release series current at investigation includes **0.20.2 (2026-08-21)**.
The 0.20 series requires **wlroots >=0.20.1 and <0.21.0**; labwc's upstream
build also requires **libinput >=1.26**. Check the complete build dependencies
if planning a compositor upgrade, not only these HDR-related version floors.
The 0.9.x maintenance branch still uses wlroots 0.19 and should not be mistaken
for the 0.20 HDR-capable series. Do not infer the distro's package version from
the latest upstream release.

`cloudplay-hdr-check` reads the installed `/usr/bin/labwc --version`, including
the linked wlroots version printed by the newer series. It diagnoses an old
version as blocked, missing/unrecognized evidence as unknown, and suitable
versions as **version floors met, HDR still unvalidated**. It is not a probe of
the active compositor process, Vulkan device/renderer, output mode or HDR state.
Its report is also recorded in the build manifest. An unknown or blocked report
does not prevent creating the initial onboarding/SDR preview image.

Even meeting both version floors is insufficient. Confirm that wlroots was
built with Vulkan support and that the running session really uses a working
Vulkan renderer on Pi 5. Validate the actual **V4L2 HEVC Main10 SAND/dmabuf import
and presentation path** across decoder, Chromium, compositor and Mesa; do not
assume successful SAND import through another renderer proves Vulkan can
present the same surfaces correctly. Validate transfer functions, color space,
bit depth, output metadata and physical display HDR in the acceptance matrix.
No unverified HDR switches, renderer environment settings, output configuration
keys or compositor-package replacements are installed by this prototype.

Sources:
- [Pinned pi-gen README](https://github.com/RPi-Distro/pi-gen/blob/74d08a337bd29da289b9aedbe5b48c79fb2e5a03/README.md)
- [Pinned stage3](https://github.com/RPi-Distro/pi-gen/tree/74d08a337bd29da289b9aedbe5b48c79fb2e5a03/stage3)
- [Pinned user-rename hook](https://github.com/RPi-Distro/pi-gen/blob/74d08a337bd29da289b9aedbe5b48c79fb2e5a03/export-image/01-user-rename/01-run.sh)
- [Stock rename-user implementation](https://github.com/RPi-Distro/userconf-pi/blob/master/rename-user)
- [Pi Trixie ARM64 package metadata](https://archive.raspberrypi.com/debian/dists/trixie/main/binary-arm64/Packages.gz)
- [Labwc release notes, including 0.20.0 HDR10](https://github.com/labwc/labwc/blob/master/NEWS.md#0200---2026-05-25)
- [Labwc HDR10 output PR #3424](https://github.com/labwc/labwc/pull/3424)
- [Labwc build dependencies](https://github.com/labwc/labwc/blob/master/meson.build)
- [Labwc version-output implementation](https://github.com/labwc/labwc/blob/master/src/main.c)
