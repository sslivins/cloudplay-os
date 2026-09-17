# Cloudplay OS — direct-to-GeForce-NOW kiosk preview

**Known physical-boot failure in image build 35168527444:** the operator
reported a localhost setup attempt followed by a black screen/cursor. Source
diagnosis reproduced a NetworkManager startup bug that can wrongly send working
Ethernet to setup. Live traces also exposed an uncaught truncated-HTTP response
that kills the setup client, and image inspection proved a root-owned private
`.config` ancestor blocks user services/browser storage. Targeted fixes are
under recovery validation. **The downloadable image
below contains that bug; build/inspection success is not physical acceptance.**
See [startup diagnostics](docs/maintenance.md#startup-diagnostics-and-known-preview-defect).

**An appliance, not the Raspberry Pi desktop.** Boot is designed to show a
**Cloudplay OS** splash with a live spinner while networking starts, then open
full-screen Chromium at **https://play.geforcenow.com/** for NVIDIA sign-in.
There is no LightDM, desktop panel, taskbar, wallpaper shell, OS account wizard,
terminal greeter or Cloudplay settings application.

The earlier desktop preview was physically booted and rejected because it
delivered the wrong experience. This redesign replaces that boot flow; previous
desktop artifacts remain historical only. **The new kiosk boot/UX is not yet
physically validated.** An image build and static inspection are not a boot
test. No claim of interactive GFN gameplay, 4K decoding or HDR output is made.

**The 1920×1080 splash preview was approved on 2026-09-16.** The current built
preview includes that exact artwork and Agora-style network onboarding,
without restoring the Raspberry Pi desktop or OS account wizard. Agora's
existing network phase is the reference. The refreshed ARM64 image has been
built, downloaded and independently inspected. **Physical boot, Wi-Fi/AP,
splash handoff and NVIDIA-login acceptance remain unvalidated.**

## Boot and runtime

- Raspberry Pi OS **Trixie ARM64**, pi-gen stages **0–2 (Lite)** plus Cloudplay.
  The pinned recipe includes the official 2026-09-15 release baseline.
- Root and fixed appliance account **`cloudplay` (UID 1000)** have locked
  passwords. No shared password, first-run user rename or sudo-group membership.
- **greetd** opens a distro PAM/login/logind session on VT7 and drops to
  `cloudplay`. Both initial and fallback sessions run the kiosk—not a greeter UI.
  PAM is retained; no `pam_permit` password-authentication workaround is added.
- **labwc** uses a root-owned minimal configuration, explicit no-op shortcuts
  instead of default terminal/menu bindings, and no desktop autostart.
  Client click-to-focus/raise remains enabled for browser sign-in windows.
  A real D-Bus session and PipeWire/WirePlumber provide session/audio plumbing.
- Chromium runs nonroot with its **normal sandbox**, `--kiosk`, native Wayland,
  `--use-angle=gles`, the reviewed compatibility extension, and no debug listener.
- Browser sessions persist in
  `/home/cloudplay/.config/cloudplay/chromium-profile` (mode 0700). There is no
  desktop keyring password prompt; the basic password store is **not encrypted
  at rest**. Anyone with physical access can use the signed-in session.
- Browser and compositor supervisors back off after failures:
  **2, 4, 8, 16, 32 seconds, then 5 minutes** after six rapid exits. Stable runs
  reset the counter. Only their own child process groups are terminated.
  Closing Chromium deliberately also relaunches it. There is no desktop escape.

## Cloudplay splash

A native Plymouth theme preserves the approved cloud/play motif, wordmark and
tagline. Following physical feedback, the source replaces only the baked footer
with **one animated spinner and live network status**. The footer-free raster
differs from the approved original only inside pixels `(876,870)–(1044,927)`;
its SHA256 is `63436f2ffa432dd5461165b47e443c670c60c6968020736d3a4b69ce39257399`.
The original approved PNG is retained for provenance. Both are 1920×1080 and
proportionally fitted without cropping. Runtime requires neither Windows fonts
nor Pillow; spinner frames are reproducible with stdlib `scripts/generate-spinner.py`.
The motif is not claimed to be unique or trademark-cleared.

The recipe selects the theme, includes the PNG and VC4 in initramfs, retains Pi's
`auto_initramfs=1`, adds `quiet splash`, suppresses the firmware rainbow/logo
and normal console/status output. A bounded 30-second readiness gate runs while
Plymouth still owns the display: connected Ethernet/saved Wi-Fi proceeds at once,
with a brief “Network connected” status. Plymouth quits **before** greetd/labwc
acquires DRM. An unprivileged image-only `swaybg` layer preserves the same branding
during compositor/browser handoff and crash backoff; it is not a desktop or
interactive setup page. Its handoff image is static, not a second spinner.
Logs remain in the journal rather than a boot terminal.

Actual display timing, early-boot flicker, first-boot resize/reboot and handoff
to Chromium must be checked on hardware. A failed boot can still expose a
kernel/emergency diagnostic; quiet flags are not a security boundary.

## Appliance network onboarding

**Ethernet DHCP is the default.** A connected Ethernet or saved Wi-Fi interface
with an assigned address skips setup and goes to GeForce NOW. On an offline
boot the source now provides a minimal network page, not a desktop or account
wizard. It allows saved networks time to reconnect before accepting changes.
An unavailable, restarting or malformed helper is **not evidence of an offline
link**: a separately bounded read-only NetworkManager probe checks actual address
readiness. Setup opens only for a functioning helper in confirmed-offline setup
state after the grace period. Otherwise the fallback is GeForce NOW, never a
dead localhost URL. If networking and the helper are both genuinely broken,
GFN may show its own offline page; diagnostics/recovery are then required.
NVIDIA remains the only account login.

- Choose the **actual country** where the device is used, select a scanned
  network (or enter a hidden SSID), choose security and enter its password.
  WPA/WPA2 Personal, WPA3 Personal, Open and OWE are supported by the source;
  enterprise/802.1X and legacy WEP need separate administrator provisioning.
- Optionally choose **Use a phone to set up Wi-Fi** on the TV. This starts a
  temporary, uniquely password-protected `Cloudplay-…` hotspot. Join using the
  TV's QR code/password, then open its captive portal or `http://10.42.0.1`.
  No shared/default hotspot password is shipped.
- One radio cannot stay in hotspot mode while joining the home network.
  The portal sends an acknowledgement before its hotspot disconnects.
  On failure, the TV reports it and offers a new hotspot code for retry.
  Phone scanning uses the results cached before AP mode; manual SSID entry
  remains available.
- Successful DHCP precedes saving the new NetworkManager profile and opening
  GeForce NOW. Failed attempts do not delete older saved networks. Ethernet
  connected during setup ends the hotspot and bypasses the wizard.
- With no Wi-Fi interface, show Ethernet instructions and keep checking for
  an address. No endless Wi-Fi scan or false password error.

Standard Raspberry Pi 5 has onboard dual-band Wi-Fi. Compute Module 5 wireless
is optional; **Lite means no eMMC**, not no Wi-Fi. Identify the actual module
and available interfaces rather than inferring wireless support from “Lite.”

The setup browser is a separate, temporary **sandboxed nonroot** Chromium
profile under the user runtime directory. It is closed and removed before
launching the persistent NVIDIA profile. The narrowly scoped network service
uses NetworkManager's D-Bus API, not passwords in process arguments, and cannot
read home directories. Its phone listener is restricted to the AP's address
**and wireless interface** and is closed before that interface joins a LAN.
It is not a LAN administration server. Network secrets are not logged.

This adapts Agora's Ethernet-first/AP/captive-portal/single-radio/retry pattern
without its CMS adoption, fleet services or framebuffer ownership. See
[architecture and provenance](docs/maintenance.md#network-provisioning-and-recovery).
The current build includes this implementation. Real Wi-Fi, phone captive
detection and boot integration remain untested.

### Optional boot-partition provisioning

Use Raspberry Pi Imager's **custom-image** option, but **skip OS customization**
that creates/renames accounts or enables SSH. The kiosk needs the fixed account,
not Imager's owner wizard. Before first boot, edit `network-config` on the
FAT boot partition for Wi-Fi using the retained Raspberry Pi cloud-init/NoCloud
support. For example, add this alongside the existing `ethernets` entry:

```yaml
version: 2
renderer: NetworkManager
ethernets:
  ethernet:
    match:
      name: "e*"
    dhcp4: true
    dhcp6: true
    optional: true
wifis:
  wlan0:
    dhcp4: true
    optional: true
    regulatory-domain: US
    access-points:
      "YOUR_SSID":
        password: "YOUR_WIFI_PASSWORD"
```

Use the correct regulatory country and interface name for the device. The
network file is first-boot provisioning, not a live settings UI. Treat it and
`user-data` as sensitive/privileged configuration. Imager's legacy `userconf`
account flow and automatic SSH customization are **not supported**. SSH and
local getty services are masked by default. Do not ship secrets in this repo.

An assigned address is not proof of internet access. No external internet probe
or CMS gates launch; upstream captive networks or outages can still leave
GeForce NOW showing a network error. The setup flow runs when the kiosk launcher
starts; it does not forcibly interrupt an active game when connectivity drops.
Restore the network and reload, or restart the appliance to revisit setup.
Physical first-boot and recovery acceptance remains required.

## Recovery and storage safety

There is no general desktop, local admin password, recovery partition or OTA.
To reset NVIDIA login, shut the appliance down and remove its browser profile
from another trusted system, or reflash. This loses saved browser sessions.
Administrative maintenance requires deliberate offline provisioning or a
separately configured trusted access method; never enable a common password.

**Never flash a card containing the running root filesystem.** Boot from a
different device first, identify the target by serial/capacity, ensure every
target partition is unmounted, then have the operator approve the write.
No build or verification command in this repository flashes/reboots a device.

## Browser and extension evidence

The image uses published **Chromium HEVC v0.4.1**:
`1:152.0.7977.82-1~deb13u1+rpt2`. Asset filenames replace `~` with `.`, so
`manifest.json` pins both the filename version and actual Debian version.
All four package digests were independently verified. The packaged executable's
BuildID is `ead10187a8df80ef5b7683ad3e113baef22016cb`.

The actual `.deb`-extracted browser passed five local **1080p30** HEVC fixtures
(8-bit, 10-bit, HDR-coded content and weighted-prediction 8/10-bit) as sandboxed
UID1000 on Pi 5. V4L2 logs, the browser's own `/dev/video19` descriptors and
image/motion checks agreed. Every fresh launch exercised the pre-keymap
keyboard guard; seven keyboard tests passed. This is **not** GFN gameplay,
4K decoding, HDR display output, or validation of this new kiosk session.

An earlier **v0.4.0** experiment streamed GFN LEGO Bricktales menus at
1920×1080/about60 fps using H265 profile1, `V4L2VideoDecoder` and
`powerEfficient=true`: +7,261 frames over 121.6 seconds, unchanged 26 startup
drops, zero freezes. It did not establish interactive gameplay. The physical
display was 1080p and account Free, so no 4K/HDR acceptance was possible.

The extension stays pinned at
`2c652315d82a126dfd9cd281ed2d16cead51e3ae`, archive SHA256
`d077a8e475ee7a426a4e4fd7484e3364e735942ba6b9be8035943b4ed1266862`.
It is root-owned at `/opt/gfn-pi-compat`, installed atomically into versioned
read-only directories. No extension runtime changes were needed for the kiosk.

## Checks, build and provenance

```sh
python3 -m unittest discover -s tests -v
python3 scripts/artifacts.py validate
```

Manually dispatch **Build unsigned ARM64 kiosk preview**. It builds on a
dedicated native `ubuntu-24.04-arm` runner, validates source pins and image
configuration, checks the Plymouth theme in initramfs, then uploads a short-lived
**unsigned** `cloudplay-os-kiosk-preview-<commit>` artifact. No public release
publishing, signing, backend or hardware deployment is performed.

For a clean committed checkout on a dedicated native ARM64 builder with the
workflow's host dependencies installed, run `sudo bash scripts/build-image.sh`.
Do not run privileged image builds on a shared production host. Only the
manifest's five verified inputs are staged, even when old packages remain cached.

Outputs under `build/pi-gen/deploy` include `.img.xz`, `SHA256SUMS`, bmap and
`provenance/`. The image contains `/usr/local/share/cloudplay/` with installed
package/source versions, input pins, extension/configuration hashes and the
kiosk export-verification report. OS apt packages and runner packages are not
snapshot-pinned: this is auditable, not fully reproducible.

**All four Chromium packages are held. Ordinary apt upgrades do not update the
browser.** Read [maintenance/security](docs/maintenance.md) and the full
[physical acceptance matrix](docs/hardware-acceptance.md).

## Current built kiosk/OOBE preview

[ARM64 build 35168527444](https://github.com/sslivins/cloudplay-os/actions/runs/35168527444)
completed successfully from clean source
`6a04ee16221732bf359ec62caa24bd2ba8e6e81e`. Download its
`cloudplay-os-kiosk-preview-6a04ee16221732bf359ec62caa24bd2ba8e6e81e`
artifact (ID `10476231163`; expires **2026-09-24 01:01 UTC**).
This is an unsigned preview artifact, not a signed/public OS release.

- Image: `image_2026-09-17-cloudplay-os-kiosk-preview.img.xz` (UTC build date)
- Size: **963,115,280 bytes** compressed; **4,269,801,472 bytes** (4,072 MiB)
  uncompressed.
- SHA256:
  `2d2a1abf18a7ed8d77bf65a61c26c746824d1672bb764a277708e18fd498c18f`

The downloaded image passed SHA256 and xz integrity checks. Independent,
read-only FAT/ext4 inspection verified the locked UID1000 account, masked
SSH/gettys, absence of desktop/wizard packages, greetd/PAM/logind wiring,
configuration/extension hashes, four held browser packages, and exact packaged
Chromium executable SHA256/BuildID. Both **firmware-loaded** `initramfs8` and
`initramfs_2712` contain the selected Cloudplay theme, script and text plugins,
fonts, DRM renderer and VC4 module.
The approved PNG is byte-identical in the repository, root filesystem and
**both firmware initramfs**. Onboarding services, dependencies, root-only code
ownership and separate sandboxed setup-profile wiring were also verified.

All **60 tests** passed. The actual image build checked its installed Python
dependencies and systemd units and exercised labwc as nonroot with a headless
backend. Earlier real builds caught and fixed pi-gen's
suppressed initramfs updates and missing `/dev/shm` submount in the build chroot;
neither check was bypassed. Necessary browser-window click focus is retained
without restoring desktop/menu shortcuts.

**This image was flashed and failed physical startup acceptance.** The source
fixes and revised spinner/network-gated handoff are not included in that artifact
and remain under operator-controlled recovery testing. Splash appearance/handoff, NVIDIA login,
input/audio, networking and session persistence still need the physical
acceptance matrix. A passing headless compositor check does not validate DRM
output or this appliance's real boot sequence.

## Historical previews — not the current image

Preserved for traceability, not recommended for the requested appliance UX:

- [Initial v0.4.0 desktop run 35138972856](https://github.com/sslivins/cloudplay-os/actions/runs/35138972856),
  source `d90abfed4c1e474f3ca85df6d5b0a95db802f55b`.
- [v0.4.1 desktop run 35151651032](https://github.com/sslivins/cloudplay-os/actions/runs/35151651032),
  source `bd238607643c039838bc5ccec856aaeabbb264f8`.
- [Pre-onboarding kiosk run 35164399916](https://github.com/sslivins/cloudplay-os/actions/runs/35164399916),
  source `9f6170a5717df90ada967a064c514bdfdfe0e608`: text-placeholder splash and
  manual Wi-Fi provisioning only; it lacks the subsequently requested changes.

The first two 4,600 MiB images retained LightDM and the Pi desktop/wizard.
A physical boot exposed that design mismatch; static “build passed” evidence
did not validate the desired UX. All earlier artifacts are preserved.

## License

Cloudplay's recipe/helpers/theme are [MIT licensed](LICENSE). Bundled OS and
browser packages retain their own licenses. Preserve their notices and satisfy
source-distribution obligations before redistribution. NVIDIA support,
affiliation or service entitlement is not implied.
