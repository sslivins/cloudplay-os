# Kiosk architecture, maintenance and trust

## Lite boot, not the rejected desktop

The old desktop preview was physically booted and rejected. The new recipe
uses only pi-gen stages 0–2 plus Cloudplay, with no stage3 desktop metapackages.
`FIRST_USER_NAME=cloudplay`, `FIRST_USER_PASS=''` creates the initial account
without a usable password. The appliance stage explicitly locks root/cloudplay,
sets the appliance shell and removes sudo/adm/disk membership.

The pinned build's `DISABLE_FIRST_BOOT_USER_RENAME=1` guard requires a nonempty
initial password. We do **not** invent a shared password to satisfy it.
Instead, the build removes only the stock `export-image/01-user-rename` stage,
which would reinstall `userconf-pi` and run `rename-user -f -s`. The appliance
stage purges the Lite-recommended `userconf-pi` package and masks its service.
No piwiz, user rename, whiptail account dialog or LightDM is shipped.

greetd 0.10.3-4 uses the distro PAM `login` stack, including `pam_systemd`,
and registers its VT/seat with logind before dropping UID/GID. The initial
session runs as `cloudplay`; the default fallback runs the same kiosk rather
than `agreety`. There is no password-authentication bypass module. greetd's
fallback is technically a greeter-class logind session, but executes only
the unprivileged kiosk, not a greeter UI. Normal operation stays in the initial
user session while its compositor/browser supervisors handle restarts.

`cloudplay-session` requires PAM's owned `XDG_RUNTIME_DIR`, then launches
`dbus-run-session` and minimal labwc. The compositor's session client imports
the Wayland environment into D-Bus/systemd and starts the user audio services.
Neither compositor nor Chromium runs as root. The fixed account has audio,
video, render and input access but no disk/admin/sudo access.

Both initial and fallback commands log through `systemd-cat`. The compositor's
root-owned config supplies explicit no-op key/mouse bindings: labwc otherwise
loads terminal/menu defaults when no binding entries exist. It does not invoke
stock desktop autostart, panels, wallpaper managers or settings tools.

The child supervisors only signal their own newly created process groups.
Short-lived failures back off 2/4/8/16/32 seconds, then pause five minutes after
six failures; runs lasting at least two minutes reset the sequence. This also
applies to intentional browser closes. No immediate crash-respawn storm or
automatic sign-out/profile deletion is performed.

## Splash and initramfs

The installed `cloudplay` Plymouth script theme uses text sprites and a dark
background. `plymouth-set-default-theme cloudplay` selects it. The recipe adds
VC4/V3D to initramfs modules, preserves `auto_initramfs=1`, adds `quiet splash`,
disables the firmware rainbow splash and ordinary console/status banners,
and overrides Plymouth quit with `--retain-splash`.

The distro greetd unit orders after `plymouth-quit-wait.service` and conflicts
with the VT7 getty. All auto-gettys are masked/disabled, so the handoff cannot
land at a normal terminal login. The export hook regenerates initramfs, then
checks that **every** kernel initrd contains the custom theme, script plugin
and VC4 driver. pi-gen's final export regenerates the same configured initrds;
the downloadable image must also be inspected before handoff.

None of these static checks proves that Plymouth renders correctly on a
specific CM5/Pi/display, that the kernel loads the expected firmware initrd,
or that the transition is flicker-free. Physical boot acceptance is mandatory.

## Network provisioning and recovery

Retain `cloud-init` and `rpi-cloud-init-mods`: the latter configures NoCloud
from `file:///boot/firmware` and NetworkManager via netplan. Default `user-data`
contains `users: []` and disables SSH password login. Default `network-config`
requests Ethernet DHCP. Wi-Fi can be provisioned before first boot using the
README's network-config example. Skip Imager account/SSH customization; the
legacy `userconf` route is intentionally removed. Arbitrary replacement
user-data is privileged physical provisioning, not untrusted kiosk input.

There is no local admin password, general desktop, terminal greeter or settings
application. Offline media maintenance or a separately provisioned trusted
admin method is required. Protect boot configuration and browser profiles.
For reflash, boot another root device and have the operator identify/unmount
the target. Never overwrite mounted/running root storage.

## Browser/extension security debt

All four v0.4.1 Chromium packages are held together. An ordinary apt upgrade
does **not** patch browser vulnerabilities. Maintainers must monitor advisories,
review replacements in the separate browser project, update all four digests
and the actual Debian version, then rebuild/retest. The image workflow does not
recompile Chromium. No automatic browser updater or maximum-age gate exists.

The browser profile is 0700 but is not encrypted at rest. `--password-store=basic`
avoids an impossible desktop-keyring unlock prompt on a locked kiosk account.
Physical access exposes the persistent NVIDIA session. Kiosk mode is UI, not
a browser navigation security boundary. Never use root or disable sandboxing.

The extension installer verifies a local reviewed commit/archive SHA256,
rejects traversal/links/nonregular files/oversized archives, stages root-owned
0555 directories and 0444 files, then atomically replaces `/opt/gfn-pi-compat`.
This is not signed OTA or verity. Retained versions allow an administrator's
offline rollback. Close the kiosk before switching versions; do not mutate the
active extension under a running browser.

## OS release provenance and HDR limits

Official Raspberry Pi OS Trixie ARM64 **2026-09-15** identifies release recipe
`2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268` (September 14 17:11:54 UTC).
Our retained pin `74d08a337bd29da289b9aedbe5b48c79fb2e5a03`
(September 16 14:45:05 UTC) is that release plus build-host fixes:
`udevadm settle -t 10`, Docker binfmt handling, ARM64 dependency naming/docs.
It does not add desktop-stage changes. It is not falsely labeled the release
commit, and default `master` is not the ARM64 branch.

The earlier built images installed kernel 6.18.50, labwc 0.20.1, wlroots 0.20.2
and Mesa 26.2.2, matching the September official stack. Apt remains
unsnapshotted; the new kiosk's inventory is authoritative for its exact versions.
Labwc is retained without the desktop for future HDR investigation.

`cloudplay-hdr-check` only checks upstream version floors: labwc >=0.20.0 and
linked wlroots >=0.20.1. HDR10 additionally requires a working Vulkan renderer,
Pi V4L2 HEVC Main10 SAND/dmabuf import, genuine GFN service entitlement/color
signaling, browser color management, KMS/HDMI metadata and physical display
verification. Version floors, HDR-coded local clips or tone-mapped SDR are not
HDR output acceptance. No unverified renderer or HDR-enabling flags are added.

## Distribution

Artifacts are unsigned developer previews. SHA256 detects corruption but is
not an independent publisher trust anchor. A signed release needs a separately
reviewed signing/key-custody/verification/revocation/recovery design. Do not
publish Wi-Fi secrets, browser cookies, tokens or identifying diagnostics.

## Verified sources

- [Pinned user creation](https://github.com/RPi-Distro/pi-gen/blob/74d08a337bd29da289b9aedbe5b48c79fb2e5a03/stage1/01-sys-tweaks/00-run.sh)
- [Pinned Lite user/groups and root locking](https://github.com/RPi-Distro/pi-gen/blob/74d08a337bd29da289b9aedbe5b48c79fb2e5a03/stage2/01-sys-tweaks/01-run.sh)
- [Stock export user rename being omitted](https://github.com/RPi-Distro/pi-gen/blob/74d08a337bd29da289b9aedbe5b48c79fb2e5a03/export-image/01-user-rename/01-run.sh)
- [greetd configuration and initial/default sessions](https://manpages.debian.org/trixie/greetd/greetd.5.en.html)
- [greetd 0.10.3-4 PAM/logind/UID-drop implementation](https://sources.debian.org/src/greetd/0.10.3-4/greetd/src/session/worker.rs/)
- [labwc session command and D-Bus environment](https://manpages.debian.org/trixie/labwc/labwc.1.en.html)
- [labwc explicit/default bindings](https://manpages.debian.org/trixie/labwc/labwc-config.5.en.html)
- [Plymouth text sprites and callbacks](https://www.freedesktop.org/wiki/Software/Plymouth/Scripts/)
- [Official September image provenance](https://downloads.raspberrypi.com/raspios_arm64/images/raspios_arm64-2026-09-15/2026-09-15-raspios-trixie-arm64.info)
- [Released-to-pinned recipe diff](https://github.com/RPi-Distro/pi-gen/compare/2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268...74d08a337bd29da289b9aedbe5b48c79fb2e5a03)
- [Labwc HDR10 support](https://github.com/labwc/labwc/pull/3424)
