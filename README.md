# Cloudplay OS — direct-to-GeForce-NOW kiosk preview

**An appliance, not the Raspberry Pi desktop.** Boot is designed to show a
**Cloudplay OS** splash, start a minimal Wayland session, and immediately open
full-screen Chromium at **https://play.geforcenow.com/** for NVIDIA sign-in.
There is no LightDM, desktop panel, taskbar, wallpaper shell, OS account wizard,
terminal greeter or Cloudplay settings application.

The earlier desktop preview was physically booted and rejected because it
delivered the wrong experience. This redesign replaces that boot flow; previous
desktop artifacts remain historical only. **The new kiosk boot/UX is not yet
physically validated.** An image build and static inspection are not a boot
test. No claim of interactive GFN gameplay, 4K decoding or HDR output is made.

**Next revision is awaiting splash-preview approval.** The current built
artifact below predates the requested Agora-style Wi-Fi onboarding. Its text
splash is an implementation placeholder, not approved final artwork. The next
revision must retain a minimal appliance network-setup flow when needed,
without restoring the Raspberry Pi desktop or OS account wizard. Agora's
existing implementation is the reference; this flow is not implemented here
yet. No final-artwork update or new image build should precede preview approval.

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

A native Plymouth script theme draws **Cloudplay OS** and “Starting your cloud
gaming session” on a dark background without external binary artwork. The
recipe selects the theme, includes it and VC4 in initramfs, retains Pi's
`auto_initramfs=1`, adds `quiet splash`, suppresses the firmware rainbow/logo
and normal console/status output, then retains the splash until greetd/labwc
take over. Logs remain in the journal rather than a boot terminal.

Actual display timing, early-boot flicker, first-boot resize/reboot and handoff
to Chromium must be checked on hardware. A failed boot can still expose a
kernel/emergency diagnostic; quiet flags are not a security boundary.

## Networking and first boot

The instructions in this section describe the **currently built artifact**.
The requested next revision adds on-device appliance Wi-Fi onboarding; manual
boot-partition provisioning must not be its only Wi-Fi setup path.

**Ethernet DHCP is the default.** The only normal on-screen login should be
NVIDIA's own web flow. Connect a supported keyboard/controller/mouse as needed.

Standard Raspberry Pi 5 has onboard dual-band Wi-Fi. Compute Module 5 wireless
is optional; **Lite means no eMMC**, not no Wi-Fi. Identify the actual module
and available interfaces rather than inferring wireless support from “Lite.”

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

If offline, Chromium may display its network-error page; the session remains
running. Restore Ethernet/network provisioning and reload. No internet probe
or OS onboarding screen gates launch. First-boot network provisioning and
offline recovery still require physical acceptance.

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

## Built kiosk preview

[ARM64 build 35164399916](https://github.com/sslivins/cloudplay-os/actions/runs/35164399916)
completed successfully from clean source
`9f6170a5717df90ada967a064c514bdfdfe0e608`. Download its
`cloudplay-os-kiosk-preview-9f6170a5717df90ada967a064c514bdfdfe0e608`
artifact (ID `10473999322`; expires **2026-09-24 00:02 UTC**).
This is an unsigned preview artifact, not a signed/public OS release.

- Image: `image_2026-09-16-cloudplay-os-kiosk-preview.img.xz`
- Size: **961,407,616 bytes** compressed; **4,261,412,864 bytes** (4,064 MiB)
  uncompressed.
- SHA256:
  `8b6ca858f4007489e93ee505c60f29eab0a1313c944e86344e24ed34b14b2aa7`

The downloaded image passed SHA256 and xz integrity checks. Independent,
read-only FAT/ext4 inspection verified the locked UID1000 account, masked
SSH/gettys, absence of desktop/wizard packages, greetd/PAM/logind wiring,
configuration/extension hashes, four held browser packages, and exact packaged
Chromium executable SHA256/BuildID. Both **firmware-loaded** `initramfs8` and
`initramfs_2712` contain the selected Cloudplay theme, script and text plugins,
fonts, DRM renderer and VC4 module.

All 32 recipe tests passed, and the actual image build exercised labwc as
nonroot with a headless backend. Real builds caught and fixed pi-gen's
suppressed initramfs updates and missing `/dev/shm` submount in the build chroot;
neither check was bypassed. Necessary browser-window click focus is retained
without restoring desktop/menu shortcuts.

**Not yet flashed or boot-tested.** Splash appearance/handoff, NVIDIA login,
input/audio, networking and session persistence still need the physical
acceptance matrix. A passing headless compositor check does not validate DRM
output or this appliance's real boot sequence.

## Historical desktop artifacts — not the kiosk design

Preserved for traceability, not recommended for the requested appliance UX:

- [Initial v0.4.0 desktop run 35138972856](https://github.com/sslivins/cloudplay-os/actions/runs/35138972856),
  source `d90abfed4c1e474f3ca85df6d5b0a95db802f55b`.
- [v0.4.1 desktop run 35151651032](https://github.com/sslivins/cloudplay-os/actions/runs/35151651032),
  source `bd238607643c039838bc5ccec856aaeabbb264f8`.

These 4,600 MiB images retained LightDM and the Pi desktop/wizard. A physical
boot exposed that design mismatch; static “build passed” evidence did not
validate the desired UX.

## License

Cloudplay's recipe/helpers/theme are [MIT licensed](LICENSE). Bundled OS and
browser packages retain their own licenses. Preserve their notices and satisfy
source-distribution obligations before redistribution. NVIDIA support,
affiliation or service entitlement is not implied.
