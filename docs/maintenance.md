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

### Official release baseline checked 2026-09-16

The latest official Raspberry Pi OS 64-bit desktop image is **2026-09-15**,
Debian **13/Trixie**, kernel **6.18.50**. Its published `.info` identifies
pi-gen commit `2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268` (committed
**2026-09-14 17:11:54 UTC**), also tagged
`2026-09-15-raspios-trixie-arm64`. This is a released-image reference, not
an assumption that current upstream HEAD is a release.

Cloudplay already pins `74d08a337bd29da289b9aedbe5b48c79fb2e5a03`
(**2026-09-16 14:45:05 UTC**), the upstream **arm64** branch HEAD at review.
It contains that release commit plus five history entries (including two
merges). The net changes are build-host fixes: `udevadm settle -t 10`,
Docker binfmt handling, ARM64 dependency naming and documentation. No image
stage, desktop or owner-onboarding recipe files differ. Upstream's default
`master` branch is not the ARM64 target.

**Decision: retain the current verified pin and Trixie config.** Replacing it
with the release tag would roll back useful build fixes rather than update the
OS. The successful first Cloudplay build already contains the new release's
kernel, firmware and desktop stack. Comparison against the official image's
package inventory found:

| Component | Official 2026-09-15 and Cloudplay first image |
| --- | --- |
| Pi 5 kernel metapackage | `1:6.18.50-1+rpt1` |
| raspi-firmware | `1:1.20260907-1` |
| labwc | `0.20.1-1+rpt1` |
| wlroots | `0.20.2-1+rpt3` |
| Mesa | `26.2.2-1~bpo13+0~rpt1` |
| rpd-wayland-core / wf-panel-pi | `1.29` / `1.31` |
| piwiz / userconf-pi | `1.8` / `0.19` |
| cloud-init | `25.2-1~bpo13+1+rpt20` |

Cloudplay's apt build also picked up newer `pcmanfm-pi` 1.7 (official 1.6),
`pishutdown` 0.42 (0.41), `wfplug-imenu` 0.10 (0.9), the three network-panel
packages at 1.18 (1.17), and `mkvtoolnix` `92.0-1+deb13u1` (`92.0-1`).
This is not byte-identical to the official desktop image: Cloudplay deliberately
uses stages 0–3 plus its appliance stage rather than official stage4, retains a
separately pinned custom browser, and resolves unsnapshotted apt repositories.
No OS-only rebuild is necessary to obtain the September release baseline;
the next coordinated browser revision still needs a fresh build and acceptance.

Sources:
- [Official downloads and release date](https://www.raspberrypi.com/software/operating-systems/)
- [September release notes](https://downloads.raspberrypi.com/raspios_arm64/release_notes.txt)
- [Official September image provenance and package inventory](https://downloads.raspberrypi.com/raspios_arm64/images/raspios_arm64-2026-09-15/2026-09-15-raspios-trixie-arm64.info)
- [Released ARM64 recipe tag](https://github.com/RPi-Distro/pi-gen/tree/2026-09-15-raspios-trixie-arm64)
- [Exact released-to-pinned recipe comparison](https://github.com/RPi-Distro/pi-gen/compare/2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268...74d08a337bd29da289b9aedbe5b48c79fb2e5a03)

### Preserving desktop and onboarding

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

The first image (run
[35138972856](https://github.com/sslivins/cloudplay-os/actions/runs/35138972856),
source `d90abfed4c1e474f3ca85df6d5b0a95db802f55b`) actually installed labwc
**0.20.1**, linked to wlroots **0.20.2**. Its embedded diagnostic reports
`version-floors-met-hdr-still-unvalidated`. This removes the old-version
prerequisite blocker for that image only, not the renderer/import/display
acceptance requirements below.

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
