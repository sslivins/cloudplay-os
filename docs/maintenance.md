# Kiosk architecture, maintenance and trust

## Lite boot architecture

The recipe uses only pi-gen stages 0–2 plus Cloudplay, with no stage3 desktop metapackages.
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
video and render access but no disk/admin/sudo or broad `input` group access.
logind supplies compositor seat access; a separate `cloudplay-gamepad` group
can access only udev-classified, non-keyboard gamepads: event nodes retain
force-feedback write access for Chromium; joydev nodes are read-only.

Both initial and fallback commands log through `systemd-cat`. The compositor's
root-owned config supplies explicit key/mouse bindings: labwc otherwise
loads terminal/menu defaults when no binding entries exist. It does not invoke
stock desktop autostart, panels, wallpaper managers or settings tools.

The child supervisors only signal their own newly created process groups.
Short-lived failures back off 2/4/8/16/32 seconds, then pause five minutes after
six failures; runs lasting at least two minutes reset the sequence. This also
applies to Home/compositor failures. A closed service browser returns to Home
without automatically reconnecting the stream or deleting sign-in data.

## Home and service lifecycle

After network onboarding, `cloudplay-start` runs `launcher/main.py`, a native
GTK3 Wayland fullscreen menu. GTK's Python bindings are runtime dependencies,
not a desktop shell. GFN keeps `/home/cloudplay/.config/cloudplay/chromium-profile`;
Xbox uses the sibling `xbox-profile`. Only GFN loads the compatibility extension.
The two HTTPS entry points are fixed in `launcher/host.py`; callers cannot supply
a URL, executable, arguments, profile path or shell command.

Each service runs in the transient **user** unit `cloudplay-stream.service`,
created with `systemd-run --user`, `ExitType=cgroup`, `KillMode=control-group`,
and a two-second graceful-stop deadline followed by SIGKILL. This tracks even
renderers which outlive or detach from Chromium's original process. Home and
Reload synchronously stop the unit; Home is not displayed as successful until
the unit is inactive. A replacement Home process first stops any surviving
owned unit, so a launcher crash cannot leave a hidden stream after recovery.
Service failure returns to Home, not a browser restart loop. Browser output
is discarded to avoid collecting service URLs, authentication state or tokens.

labwc binds Ctrl+Alt+Home on release to `cloudplay-home`, with
`overrideInhibition="yes"` so a service's keyboard-shortcut lock cannot suppress
the recovery binding. The helper sends only
`home` to a mode0600 Unix sequenced-packet socket inside the user's mode0700
runtime directory. The listener also checks `SO_PEERCRED`, bounds packet size,
client waits and work per tick. It cannot launch services directly: the command
opens the native Stay/Reload/Home confirmation over the service, independently
of Chromium, pointer lock, page JavaScript or error pages. Stay is the default.
There is **no HTTP launcher/control endpoint** and no browser-visible token:
web origins cannot access this IPC, so it adds no CORS/CSRF surface. The root
network helper remains networking-only on 8765, with its existing protections.
Same-user native code is trusted; this is not a boundary against a compromised
cloudplay account or an escaped Chromium sandbox.

`launcher/gamepad.py` opens at most four udev-selected gamepads read-only,
checks evdev capabilities again, rejects keyboard interfaces and reads at most
64 events per device per 50ms tick. It never opens keyboard nodes or grabs the
seat. Devices rescan every three seconds; disconnect, event loss, permission
failure and missing controllers leave keyboard navigation available. D-pad
and A/B navigate Home; Select/Back + Start/Menu held for two seconds opens
confirmation. Holds fire once per release, queued-event saturation cannot
infer a hold, and navigation must return neutral when the menu opens.
The combo can also reach the running game; this is not a pause mechanism.

Export verifies installed dependencies, launcher/control/rule hashes, profile
ownership, group restrictions and compositor binding. A headless labwc run
exercises the real GTK menu with synthetic services (never provider sign-in);
an explicit success record is required even if labwc exits zero after a failed
session client. `tests/test_launcher.py` covers IPC, lifecycle and input logic.
On Linux, `CLOUDPLAY_SYSTEMD_TEST=1 python3 -m unittest discover -s tests -p
'test_launcher.py'` additionally tests real user-cgroup cleanup with a uniquely
named synthetic unit. This does not prove Pi seat focus, controller mappings,
login persistence with providers or gameplay. Both service flows require the
hardware acceptance checklist; Xbox remains unvalidated on Pi.

## Splash and initramfs

The `cloudplay` Plymouth theme uses a 1920×1080 background with a clear footer.
One 12-frame spinner sits above a separate live network-status line. Other
sizes use centered aspect-preserving scaling, not cropping.
Asset checksums are tested and recorded with configuration hashes. Runtime text
uses the installed Plymouth label/font support, not Windows fonts or Pillow.
`scripts/generate-spinner.py` reproducibly creates the tiny PNG frames using
only Python's stdlib.
No uniqueness or trademark clearance is asserted.
`plymouth-set-default-theme cloudplay` selects it. The recipe adds
VC4/V3D to initramfs modules, preserves `auto_initramfs=1`, adds `quiet splash`,
disables the firmware rainbow splash and ordinary console/status banners,
and overrides Plymouth quit with `--retain-splash`. The startup gate orders
after the network helper but **before both Plymouth quit units and greetd**.
It must not wait for `cloud-final.service`: that service runs after
`multi-user.target`, which already waits for the early splash gate. That cycle
causes systemd to skip the gate. greetd independently waits for cloud-init
finalization. The gate waits up to 30 seconds for actual network readiness
(40-second unit hard limit), then briefly displays the result. It never keeps
Plymouth's DRM ownership while labwc starts. The same background is rendered by
an unprivileged, image-only `swaybg` layer beneath the browser, covering handoff
and crash-backoff gaps without adding a desktop or interactive GUI.

The distro greetd unit orders after `plymouth-quit-wait.service` and conflicts
with the VT7 getty. All auto-gettys are masked/disabled, so the handoff cannot
land at a normal terminal login. The export hook regenerates initramfs, then
checks that **every** kernel initrd contains the custom theme, script plugin
and VC4 driver, plus the background, all spinner frames and label plugin.
pi-gen's final export regenerates the same configured initrds;
the downloadable image must also be inspected before handoff.

The image verifier checks the complete `graphical.target` boot graph, including
target dependencies. It rejects ordering-cycle diagnostics even if
`systemd-analyze verify` exits successfully after dropping a job.

None of these static checks proves that Plymouth renders correctly on a
specific CM5/Pi/display, that the kernel loads the expected firmware initrd,
or that the transition is flicker-free. Physical boot acceptance is mandatory.

## Network provisioning and recovery

The new `onboarding/` source is a network-only adaptation of Agora phase one.
Reference revisions inspected read-only:

- `sslivins/agora` `24506aa86c629e87f195c8ab4e62d1079dc4ba9b`:
  `provision/service.py`, `network.py`, `dns.py`, `app.py`, `display.py`,
  `launch_oobe.sh`, and `tests/test_oobe_flow.py`.
- `sslivins/agora-os` `0eec364f2844ed05b7c316140354fb6a02cac970`:
  `pi-gen-overlay/stage-agora/01-install-agora/00-run.sh`.

Retained patterns: existing Ethernet/Wi-Fi wins; activation is not success
without an assigned address; DNS redirect and a listening captive portal
precede AP activation; one radio switches from AP to station; failed setup
returns to a retry flow; absent Wi-Fi produces Ethernet instructions.
Do **not** copy Agora's CMS/adoption phase, service teardown, root display
ownership, trusted APT, SSH policy or fixed HDMI mode. Its open AP and
password-bearing command arguments/event logs are not used here.

`cloudplay-network.service` runs a small root network helper. NetworkManager
D-Bus receives credentials directly, never via `nmcli` arguments. A new
station profile starts in memory and is saved by NetworkManager only after
successful activation/address assignment. Failure removes only that newly
created profile, not previously saved networks. A selected regulatory country
is persisted root-only under `/var/lib/cloudplay-network` and reapplied on boot.
The radio unblock unit follows Agora's boot pattern; NetworkManager networking
and wireless are explicitly enabled. No board-name heuristic disables Pi5 Wi-Fi.

The service cannot access `/home` or `/run/user` (`ProtectHome=yes`) and has
a read-only filesystem except its state and own DNS configuration directory.
It receives only network/bind capabilities. The helper exposes no shell/command
endpoint and grants no administrator privileges. The Wi-Fi portal does not use
the development SSH password. Loopback port 8765 serves the local UI.
Mutation requires the exact Host, same Origin and a per-process token, bounded
JSON input, supported security settings and an offline idle state. Untrusted
strings and credentials are not logged or echoed in errors; no CORS access
is granted. Request concurrency and command waits are bounded.

The optional phone hotspot requires a country selected on the TV, generates
fresh WPA credentials, and uses an in-memory volatile NM profile whose
activation is bound to the service's private D-Bus connection. The phone HTTP
listener binds `10.42.0.1:80` with `SO_BINDTODEVICE` on the actual Wi-Fi device
and `IP_FREEBIND` so it can listen **before** AP activation. This is intentionally
not `0.0.0.0`. The DNS drop-in is created first; after submission both listener
and AP are removed before station activation. Failures recreate phone setup;
Ethernet recovery ends it. Service exit disconnects the volatile AP, and
`ExecStopPost` removes only Cloudplay's DNS drop-in. Phone clients cannot read
the hotspot password from the API; it is displayed only on the local TV.

The normal labwc session launches a temporary sandboxed setup browser only
when needed. It uses its own process group/runtime profile, with no extension,
sync or persistent NVIDIA cookies. On readiness it closes only that owned
group, removes its temporary profile and executes Cloudplay Home.
Normal supervisor backoff still applies. No root framebuffer
renderer competes with labwc, and no CMS/player/Agora process is controlled.

Source tests cover state gates, real local HTTP endpoints, Host/Origin/token
rejection, secret handling, AP lifecycle ordering/cleanup, typed D-Bus settings,
DHCP checks and profile persistence/deletion boundaries. These do not replace
actual NetworkManager/Wi-Fi/captive-portal or boot acceptance.

Retain `cloud-init` and `rpi-cloud-init-mods`: the latter configures NoCloud
from `file:///boot/firmware` and NetworkManager via netplan. Default `user-data`
contains `users: []`; the development-access helper sets `ssh_pwauth: true`
only when the explicit preview SSH flag is enabled (false otherwise).
Default `network-config`
requests Ethernet DHCP. Wi-Fi can be provisioned before first boot as described
below. Skip Imager account/SSH customization; the
legacy `userconf` route is intentionally removed. Arbitrary replacement
user-data is privileged physical provisioning, not untrusted kiosk input.

There is no general desktop, terminal greeter or settings
application beyond network-only onboarding. Offline media maintenance or a separately provisioned trusted
admin method is required when the temporary development login is disabled.
Protect boot configuration and browser profiles.
For reflash, boot another root device and have the operator identify/unmount
the target. Never overwrite mounted/running root storage.

### Optional boot-partition provisioning

Before first boot, edit `network-config` on the FAT boot partition. Keep the
Ethernet entry and add Wi-Fi, for example:

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

Use the actual interface name and regulatory country. These files contain
privileged configuration and potentially Wi-Fi credentials; protect access.
This is first-boot provisioning, not a live settings interface.

## Startup diagnostics and known preview defect

The first physical boot reported for image `35168527444` (SHA256
`2d2a1abf18a7ed8d77bf65a61c26c746824d1672bb764a277708e18fd498c18f`)
attempted localhost setup and then showed a black screen/cursor. Image
readback matched before boot. Live diagnostics identified the failure paths
below; targeted corrections restored direct browser startup across a reboot.
New image revisions still require their own hardware acceptance.

A **source-level cause of incorrect setup selection is reproduced**:
NetworkManager 1.52.1 `src/core/nm-manager.c:impl_manager_enable` rejects a
redundant `Enable(true)` with `AlreadyEnabledOrDisabled`. The shipped helper
called it unconditionally before its first readiness snapshot. On an already
enabled manager, this marks the helper failed even with working Ethernet.
The old log only said `Cloudplay network service unavailable (DBusException)`;
the service then restarts. Earlier mock-based tests did not model this API
error and therefore missed it.

The source fix checks an existing link first, leaves connected networking
alone, enables only disabled settings and handles the specific concurrent-enable
race. Error diagnostics include a validated D-Bus error **name**, never its
potentially credential-bearing message. Protocol-faithful tests reproduce the
old failure; separate launcher tests exercise direct GFN, temporary-browser
cleanup/handoff and failed-child propagation into supervisor backoff.

Diagnostics now also attach the static operation, for example
`DBusException:org.freedesktop.NetworkManager.AlreadyEnabledOrDisabled@org.freedesktop.NetworkManager.Enable`
or `...@SystemBus.connect`. The original exception type/identity is preserved
for specific race handling. No method arguments, settings dictionaries,
SSIDs, passwords or exception messages are logged. A class-only message on a
test SD may still be the original service; compare installed hashes with the
coordinated hotfix manifest before concluding the corrected source failed.

Subsequent live evidence established two additional concrete failure paths:

- `ready()` received `http.client.IncompleteRead(0 bytes read, 5117 more
  expected)` when the restarting helper closed an HTTP response. That exception
  is an `HTTPException`, not either of the previously caught `OSError` or
  `ValueError`. It escaped the client, whose cleanup killed its setup browser.
  Real local HTTP reproduced the exact failure against shipped source; the
  fix treats incomplete/protocol-failed replies as retryable and validates the
  JSON shape rather than terminating the client.
- The exact shipped image has `/home/cloudplay/.config` owned by **root:root,
  mode0700**, although its home and `.config/cloudplay` leaf belong to UID1000.
  GNU `install -d` assigned the requested owner only to the final leaf, leaving
  the created parent owned by the build user; pi-gen's final export then
  `chmod 700` on that parent. This blocks UID1000 traversal and matches the live
  systemd-user permission errors, also preventing browser configuration/profile
  setup. The recipe now explicitly owns all three directories, verifies their
  UID/GID/modes, and performs write probes as the actual appliance user.
  Recovery needs only a verified **nonrecursive** ownership repair of the
  `.config` ancestor; preserve its contents and the NVIDIA profile.

These defects explain the observed startup failure. Repeated client exits
can produce a five-minute supervisor pause after six failures. Look for
`Cloudplay child exited ...; retry in 300s`, compositor/session exits and
Chromium stderr. Startup phase messages distinguish temporary setup
launch from persistent GFN launch. The core diagnostic fixes change no browser
version/flags, sandbox or persistent NVIDIA profile.

The subsequent UX fix in `onboarding/readiness.py` distinguishes helper failure
from actual offline networking. A root-owned `/run/cloudplay-startup/decision.json`
carries the just-completed boot gate into the user session (60-second validity;
offline decisions are rechecked). If the helper is delayed or broken, a separate
read-only `Network.snapshot()` child probes real address readiness with a
two-second **whole-process** timeout, including D-Bus connection/introspection.
It never calls `Enable`, `Set`, scan or activation methods. A functioning
confirmed-offline helper may request OOBE after the grace period; unknown/error
states fall back to Home instead of a dead localhost page. This does not
pretend connectivity succeeded: a selected service can still show its offline
page, from which the host Home shortcut remains available. The `cloudplay-startup` journal
identifier records safe boot phases; it does not log addresses or credentials.

UX validation includes Windows/Linux regressions, actual
Plymouth 24.004.60 ARM64 parser acceptance (and rejection of an invalid control),
and native systemd dependency validation against units extracted from the exact
failed image. The latter uses executable placeholders for path checks, not
service execution; none of these are a physical boot test. Manual installation must include
the declared `swaybg` dependency, all runtime modules/theme assets, startup-unit
enablement and regeneration of **every** firmware initramfs; source copies alone
do not update the early-boot splash.

**The failed boot's journal was volatile and is no longer recoverable after
reboot.** Although the exact shipped image has `/var/log/journal`
(root:systemd-journal, mode2755) and a commented `Storage=auto` in the main
configuration, its vendor drop-in
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` sets
`Storage=volatile`. The operator confirmed an empty journal directory on the
recovered SD. Inspect the merged settings from all configuration directories,
not just the main file or the presence of a journal directory.

The tightly coupled diagnostic update makes persistence explicit, caps system
journals at 64 MiB (retaining 128 MiB free), caps runtime logs at 16 MiB and
syncs every 15 seconds with seven-day retention. This reduces, not eliminates,
the loss risk on power failure; prefer an orderly shutdown or `journalctl --sync`
when recovery access is available. It does not send logs to the boot console.
The helper explicitly uses journal stdout/stderr with identifier
`cloudplay-network`; browser/session output keeps `cloudplay-session`.
The filename `cloudplay-diagnostics.conf` sorts after the vendor's
`40-rpi-volatile-storage.conf`; the later `syslog.conf` changes only forwarding,
not storage. `systemd-analyze --root=... cat-config systemd/journald.conf`
against the extracted shipped configuration proved **volatile before the
patch and persistent afterward**, with the 15-second sync and 64 MiB cap.
The image verifier now asserts the effective merged settings, not merely
the presence of the desired file or directory.

After the operator boots separate storage and mounts the SD root **read-only**,
inspect, for example:

```sh
journalctl --directory=/mnt/cloudplay/var/log/journal --list-boots --no-pager
journalctl --directory=/mnt/cloudplay/var/log/journal \
  -u cloudplay-network.service -u NetworkManager.service -u greetd.service --no-pager
journalctl --directory=/mnt/cloudplay/var/log/journal -t cloudplay-session --no-pager
```

Substitute the actual read-only mount path; these commands do not mount or
modify it. The session/browser stderr is inherited through `systemd-cat` with
identifier **cloudplay-session**, so querying only `greetd.service` can miss
it. There is no configured browser log file or debug listener. Also inspect
`/var/log/cloud-init.log`, `/var/log/cloud-init-output.log` and, if present,
coredump metadata. Do not publish raw cores, NVIDIA profiles, cookies or
unredacted network credentials.

The failed historical image masked SSH/gettys; later source now explicitly
enables development SSH. The setup service remains loopback-only unless the
private phone AP is explicitly started. Recovery/targeted patching is an operator
action; keep original files and record patch hashes because a hotfix makes
the image differ from its shipped provenance. No automatic reflash or rebuild
is part of this diagnosis.

### Explicit temporary development SSH

GitHub-built development previews use public **`cloud` / `cloud`** SSH credentials.
`CLOUDPLAY_DEVELOPMENT_SSH=1` is the deliberate
development default. A separate UID above1000 owns that login; the browser stays
UID1000, password-locked, nonadmin and SSH-denied. Root remains locked/SSH-denied.
`cloud` joins the normal sudo group with an explicit `PASSWD: ALL` rule, never
`NOPASSWD`. This permits `sudo journalctl` and maintenance with password `cloud`.
Treat this as public administrative access: trusted development LAN only, no
Internet exposure, and remove it before production.

The appliance stage runs `cloudplay-development-ssh 0|1` after the ordinary
pi-gen SSH-disable stage. Its early `00-cloudplay-access.conf` fixes effective
OpenSSH policy and its cloud-init setting agrees, avoiding first-boot reversal.
It enables the normal SSH service, not socket activation. Existing operator
keys are not removed. On a patched test SD, inspect other `AllowUsers` or
authentication overrides before relying on the new login; verify `cloud` in a
second connection before ending the existing diagnostic session. The helper
does not start/restart services or interfere with an in-flight diagnostic.

Set the build flag to0 for a fresh image without the account or listener.
The runtime disable path locks an existing development account, removes its
sudo grant and masks SSH startup while preserving home data. It does not revoke
live sessions or stop a running listener; those are separate operator actions.
Export checks test the actual password hash with libcrypt (without recording
the hash), account separation, password-required sudo, effective `sshd -T`
policy, service enablement/masks, and cloud-init agreement. Verify login on
each new image; a configured policy alone does not establish successful access.

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
