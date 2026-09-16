# Cloudplay OS — Pi 5 developer preview

An **image-built, hardware-unvalidated prototype** of a Raspberry Pi OS Trixie
appliance that opens GeForce NOW after normal owner onboarding. **4K60 and HDR
are product goals, not delivered capabilities.** Earlier lab evidence was
1080p60 SDR only, on a different configuration; this image and extension have
not passed acceptance. A separate Pi 5 experiment used normal UID1000,
sandboxed Chromium and the MAIN-world compatibility extension to stream the
GFN LEGO Bricktales demo: H265 profile 1, `V4L2VideoDecoder`,
`powerEfficient=true`, 1920×1080 at about 60 fps. Over 121.6 seconds it decoded
7,261 additional frames; 26 startup drops remained unchanged and freeze count
was zero. This includes menus and is **not proof of interactive gameplay** or
this image's acceptance. The test display is 1080p and the account is Free;
neither 4K nor HDR acceptance is possible on that setup.
NVIDIA affiliation/support is not implied.

**Known browser blocker:** v0.4.0 has an unresolved Wayland keyboard-state
startup crash (`xkb_state_update_mask`) seen in a separate Sway diagnostic
configuration. A disposable `about:blank` reproduction, without GFN or the
extension, observed `wl_keyboard.enter` followed by modifiers before any
keymap arrived. A conditional GDB breakpoint confirmed a null state pointer
passed to `xkb_state_update_mask`; this is not merely a keyboard-absence
hypothesis. A corrected browser build is pending and is **not included** in
this image. The stock labwc/physical-keyboard path remains untested, and a
successful image build does not establish that startup or input works.

## First image build — 2026-09-16

[ARM64 workflow run 35138972856](https://github.com/sslivins/cloudplay-os/actions/runs/35138972856)
succeeded at source commit `d90abfed4c1e474f3ca85df6d5b0a95db802f55b`.
The unsigned artifact is
`cloudplay-os-unsigned-preview-d90abfed4c1e474f3ca85df6d5b0a95db802f55b`
(CI retention expires **2026-09-23**). It contains
`image_2026-09-16-cloudplay-os-preview.img.xz`, `SHA256SUMS`, a bmap, and
`provenance/`. The image is 1,131,860,020 compressed bytes, 4,600 MiB unpacked:

```text
ba0909794e50419f66a457ff1883325b17778de587491e7575aa8a04ad82c680  image_2026-09-16-cloudplay-os-preview.img.xz
```

The downloaded checksum and xz integrity passed. Read-only filesystem
inspection confirmed embedded provenance matches the external copy, all four
browser packages have the pinned version and hold, the Chromium sandbox helper
is root-owned mode 4755, and the extension is root-owned mode 0555/0444.
LightDM selects the stock labwc wizard session; root, placeholder owner and
wizard passwords are locked, SSH is not enabled, and both wizard and guarded
Cloudplay autostart files are present.

The built desktop reports **labwc 0.20.1 / wlroots 0.20.2**: HDR version floors
are met, but Vulkan rendering, SAND import, real service entitlement and
physical HDR output are still unvalidated. **Nothing has been flashed or
boot-tested.** These static checks do not pass the physical acceptance matrix.

This image already includes the **2026-09-15 Raspberry Pi OS release baseline**.
Its exact pi-gen pin contains the released ARM64 recipe plus build-host fixes;
kernel, firmware, labwc, wlroots and Mesa match the official image. See the
[release comparison and decision to retain the current pin](docs/maintenance.md#official-release-baseline-checked-2026-09-16).

## Display target

The intended target is **3840×2160 at 60 fps with genuine HDR display output**.
The preview does not cap browser/display resolution or remove GFN high-resolution
settings. The stock Pi desktop is retained for setup and recovery, but its
labwc/compositor-to-display HDR path is **unproven**. Main10 decoding or decoded
HDR metadata alone does not demonstrate HDR presentation: the entire browser,
compositor, KMS/HDMI and physical display chain must pass acceptance. A 4K
desktop, HDR-capable decoder or tone-mapped SDR picture is not that result.

Upstream labwc **0.20.0+** added HDR10 with the **Vulkan renderer** and requires
**wlroots 0.20.1+**. This makes labwc a viable long-term path; it does not imply
the stock Pi packages contain those versions or that Pi SAND buffer import
works end to end. Run `cloudplay-hdr-check` on the image for a read-only,
version-gated prerequisite report. It changes nothing and never reports HDR
as validated. See [the concrete HDR prerequisites](docs/maintenance.md#labwc-hdr-prerequisites).

## What is here

- Pinned ARM64 pi-gen stages 0–3 plus a real appliance stage, preserving stock
  Pi desktop/labwc, display controls, Wi-Fi/Bluetooth UI and first-boot wizard.
- Existing Chromium HEVC v0.4.0 packages (152.0.7977.75), SHA256 checked; **no
  browser rebuild**. Generic V4L2 HEVC Main/Main10 SAND patches are not GFN-
  specific and do not establish end-to-end HDR.
- Separate root-owned, user-read-only MV3 `gfn-pi-compat` extension at
  `/opt/gfn-pi-compat`. Its capability gating and this Chromium build's
  `--load-extension` behavior remain mandatory physical-test items.
- Nonroot sandboxed Chromium, per-owner persistent profile mode 0700, no
  remote debugging, SSH disabled, no shared owner password.
- Manual ARM64 GitHub Actions image build; short-lived **unsigned CI artifacts
  only**, image checksums and installed package/source provenance. No release
  publishing, backend, fleet control, A/B, automatic OTA or signing setup.

## First boot and recovery

Flash a **spare** card with Raspberry Pi Imager's custom-image option. Boot with
a keyboard/mouse, monitor and Ethernet or Wi-Fi. Stock piwiz creates/renames the
owner and sets an owner-chosen password; configure country, networking and
display there. Do not enable SSH or bake credentials into a public image.
Imager customization/cloud-init is retained, but must be acceptance-tested
alongside the uncustomized wizard path. First-boot package updates may require
networking and a reboot.

The browser deliberately does **not** start in the wizard account, while
`piwiz.desktop` exists, or outside Wayland. After onboarding and a normal owner
desktop login it starts fullscreen, once per session. **F11** leaves fullscreen;
**Alt+F4** closes Chromium to the desktop. There is no respawn loop. Use the
desktop panel for networking/audio/display and the application menu's
**Cloudplay Settings and Recovery** to disable/enable next-login autostart.
Offline launch does not prevent access to these controls. GFN credentials and
cookies stay in `~/.config/cloudplay/chromium-profile`.

For keyboard recovery, switch to a local VT (Ctrl+Alt+F2), log in as the owner,
then `mkdir -p ~/.config/cloudplay; touch ~/.config/cloudplay/desktop-only`.
Log back into the desktop. If the desktop or storage is broken, reflash;
there is no recovery partition. Browser login is lost on reflash.

Appliance desktop autologin is deliberate: physical access exposes your signed-
in session. Disable autologin with stock `raspi-config` if needed; GFN then
starts after manual owner login.

## Pinned inputs and future updates

`manifest.json` pins extension source commit
`2c652315d82a126dfd9cd281ed2d16cead51e3ae` and its codeload archive SHA256
`d077a8e475ee7a426a4e4fd7484e3364e735942ba6b9be8035943b4ed1266862`.
No extension release is required. For future updates, review the published
source, put its full 40-character commit in `extension.commit`, download that
commit's codeload tarball and record its checked SHA256 in `extension.sha256`:

```sh
mkdir -p build/review
COMMIT='<reviewed-full-commit>'
curl --fail --location "https://codeload.github.com/sslivins/gfn-pi-compat/tar.gz/$COMMIT" \
  -o build/review/extension.tar.gz
sha256sum build/review/extension.tar.gz
python3 scripts/artifacts.py validate
```

Review source, not just a checksum from the same untrusted download. There is
no fallback to a branch/tag/latest. An unavailable commit or digest mismatch
stops the build. GitHub archive regeneration can require deliberate digest
review. Small CI also permits the explicit unfinalized sentinel pair for early
source review; image CI always requires finalized commit and digest pins.

## Checks and build

```sh
python3 -m unittest discover -s tests -v
python3 scripts/artifacts.py validate --allow-unset
find scripts stage-cloudplay -type f \( -name '*.sh' -o -name 'cloudplay-start' -o -name 'cloudplay-settings' \) \
  -print0 | xargs -0 -r -n1 bash -n
bash -n config
```

After pin finalization and a reviewed source commit, manually dispatch
**Build unsigned ARM64 preview**. It uses public `ubuntu-24.04-arm`, not x86
emulation. This validates the image recipe and produces workflow artifacts;
even a successful build does not validate boot or physical streaming/HDR and
does not create a signed public release. Expect substantial disk/time use; runner capacity is a practical
prototype constraint. Never run this privileged recipe on a shared production
host. For a dedicated native ARM64 Debian/Ubuntu host, install the dependencies
listed in `.github/workflows/build-image.yml`, then:

```sh
sudo bash scripts/build-image.sh
```

Use a clean committed checkout and fresh `build/pi-gen` directory. Build outputs
are in `build/pi-gen/deploy`, including `SHA256SUMS` and `provenance/`. The image
contains `/usr/local/share/cloudplay/{build-manifest.json,packages.tsv}` with
exact installed binary and source package versions, apt sources, pi-gen/browser/
extension pins, extension file hashes and builder details. OS apt and runner
packages are **not snapshot-pinned**: this is auditable, not fully reproducible.

**Critical maintenance warning:** all four Chromium packages are apt-held.
Normal OS upgrades do **not** deliver browser security fixes. Read
[maintenance and trust](docs/maintenance.md) before using real credentials.
Read [hardware acceptance](docs/hardware-acceptance.md) before claiming any
supported stream resolution, frame rate, decoder or HDR output.

## License

The Cloudplay recipe and helpers are [MIT licensed](LICENSE), consistent with
the separate compatibility extension. Bundled Raspberry Pi OS, Chromium and
other packages retain their own licenses; this license does not relicense the
image or grant rights to NVIDIA services. Preserve package copyright notices
and satisfy applicable source-distribution obligations before redistributing
images beyond the experimental CI artifact.
