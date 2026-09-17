# Kiosk hardware acceptance

Use this checklist for each candidate image. Record each item as not-run,
pass or fail with evidence; a successful build does not establish hardware
compatibility. Ethernet startup and direct browser launch have been exercised
on a corrected CM5 test installation, but that does not pass this matrix for
a newly built image or establish gameplay, Wi-Fi or HDR support.

Record image SHA256/source commit, build-manifest/kiosk-verification reports,
actual kernel/Mesa/labwc versions, board (including CM5 versus Pi5), RAM, boot
media serial, power/cooling, peripherals, monitor/EDID, cable/port, GFN account
tier, game and region. Redact credentials, session tokens and public IPs.

## Flash safety and boot UX

- Operator boots a **different root device**, identifies the intended spare
  card by serial/capacity, confirms every target partition is unmounted and
  explicitly approves the destructive write. Never flash running root.
- Verify checksum and perform readback verification. Preserve the known-good
  prior media/artifacts. No automatic flashing is included in the build.
- Cold boot shows **Cloudplay OS** branding, not the stock Pi desktop, rainbow
  screen, account wizard or normal terminal banner.
- Compare the main art against the current background asset. Confirm one
  live spinner above a separate status line, no static dots or duplicate text,
  native-size appearance at 1080p and no cropping/distortion at other sizes.
- Check first-boot filesystem resize/reboot and subsequent boots. Theme appears
  from the selected initramfs, hands off to Chromium and has no long black gap.
- With working networking, Chromium opens fullscreen at
  `https://play.geforcenow.com/`; NVIDIA sign-in is the only requested login.
  Cold Ethernet and saved-Wi-Fi boots show branding/network progress, then GFN:
  **no transient interactive setup page or localhost connection error**.
  Confirm Plymouth releases DRM before labwc and the matching image-only
  transition layer covers compositor/browser startup and restart backoff.
  If network setup is needed, show only the minimal appliance Wi-Fi flow,
  never a desktop, OS account/password wizard or terminal.
- No LightDM, graphical/text greeter, taskbar, desktop panel, wallpaper shell
  or Cloudplay settings window appears. Browser login popups still work.
- Confirm `cloudplay` UID1000 owns the compositor/browser; greetd has opened
  a real seat0/VT7 PAM/logind session and a D-Bus user session exists.
- Root/cloudplay passwords stay locked; cloudplay has no sudo/adm/disk group
  and neither account accepts SSH. No ordinary getty is active.
- Development mode only: `ssh cloud@DEVICE_IP` accepts the explicitly public
  password `cloud`; `sudo` requires that password. Verify the account differs
  from browser UID1000, host keys are device-specific, and a reboot/cloud-init
  does not disable the intended login. Keep this preview on a trusted LAN.
- With `CLOUDPLAY_DEVELOPMENT_SSH=0`, a fresh image has no development account
  or SSH listener. Check both modes before removing the temporary exception.

## Input, network, audio and persistence

- Ethernet DHCP works with a fresh card. The browser does not wait for a
  network wizard when already connected. Complete real NVIDIA and optional
  federated/2FA login flows.
- On a fresh wireless-capable board without Ethernet, the appliance onboarding
  can list networks, accept credentials, report a wrong password and retry,
  then hand off to GeForce NOW. Test hidden SSIDs and the correct regulatory
  country. Saved networking survives reboot without rerunning onboarding.
- Test standard Pi5 onboard Wi-Fi and, separately, a CM5 configuration with
  no wireless interface: no endless scan/spinner or false “wrong password.”
  CM5 Lite denotes absence of eMMC, not absence of wireless.
- Test Ethernet connected during setup, delayed DHCP, dropped connectivity
  and power interruption during configuration. Network setup must not require
  a shared administrator password or an unsandboxed/root browser.
- With healthy Ethernet, restart/fail the setup helper and return truncated
  status responses: the actual-address fallback must still select GFN.
  A genuinely offline, functioning helper opens OOBE after the bounded grace;
  a broken helper never launches its dead localhost URL as a fallback.
- Verify the setup endpoint is not reachable from another LAN machine;
  the optional phone portal is accessible only over the temporary private AP,
  with a newly generated password shown locally. Test QR joining and Apple,
  Android and Windows captive detection. The listener must close before the
  radio joins the home network, including error/stop paths.
- Verify wrong-origin/missing-token mutations are rejected, both before and
  after service restart. The old TV token must recover on the next submission.
  NVIDIA cookies are not exposed to the setup service, and Wi-Fi credentials
  do not appear in logs, URLs, command-line arguments or saved browser forms.
- Repeat with first-boot `network-config` Wi-Fi provisioning and correct
  regulatory domain. Test no-network boot, later network recovery and reload.
  Imager account-renaming/SSH customization is not supported.
- Test keyboard typing, pointer lock, mouse, wired/Bluetooth controllers,
  hotplug/disconnect/reconnect and actual gameplay input.
- Confirm PipeWire/Pulse compatibility socket and WirePlumber run as the
  appliance user; test HDMI/USB audio, sync and reconnection.
- Browser profile is mode0700 and NVIDIA login persists across normal reboot.
  Sign-out works. Offline profile deletion and manual reflash clear login.
- Check keyboard shortcuts do not launch terminals, a root menu or desktop
  shell. Kiosk mode is not a security boundary against arbitrary browsing.
- Close/crash the browser and compositor separately. Their own supervisors
  relaunch with documented bounded backoff, without touching other processes.
  After repeated failures, verify the five-minute pause rather than a tight
  crash loop. Recovery must not require a desktop interface.
- Test monitor unplug/replug, mode changes and power loss. No claim of
  flicker-free handoff or reliable recovery until these tests pass.

## Browser/extension and evidence boundaries

- `chrome://version` shows the exact v0.4.1 packaged executable/arguments.
  `chrome://sandbox` confirms normal sandboxing; no root/no-sandbox/debug port.
- Reviewed MV3 extension loads from root-owned `/opt/gfn-pi-compat`; test true
  HEVC capability with support present and deliberately absent.
- Confirm real provider login without certificate bypasses or entitlement
  spoofing. An extension cannot grant a service tier/region capability.

Separate evidence already exists for packaged browser v0.4.1, BuildID
`ead10187a8df80ef5b7683ad3e113baef22016cb`: seven keyboard tests and five
local **1080p30** HEVC fixture/image/motion tests passed as sandboxed UID1000,
with V4L2 logs/device descriptors and the pre-keymap guard exercised on each
fresh launch. That ran in a different session, not this kiosk image.
HDR-coded content there was not proof of HDR display output.

Earlier v0.4.0 live GFN evidence negotiated H265 profile1 at 1080p/about60fps,
but did not prove interactive gameplay and is a different browser revision.

## Stream/display matrix

1. **1080p60 SDR**: prove real GFN negotiated codec/dimensions/rate and active
   V4L2 hardware decode on this image, plus usable interactive controls/audio.
2. **3840×2160 at 60fps SDR**: prove actual inbound/decoded dimensions and
   measured rate, not desktop resolution or a menu option. Record dropped
   frames, bitrate, latency, CPU/GPU load and temperature over 30+ minutes.
3. **3840×2160 at 60fps HDR**: all preceding evidence plus all conditions below.
   Mark blockers rather than treating decoded HDR metadata or SDR tone mapping
   as HDR output.

HDR acceptance requires:

- Genuine service entitlement/region/game and a capable display/link.
- Installed/running labwc >=0.20.0 with linked wlroots >=0.20.1; preserve
  `cloudplay-hdr-check` output. Floors alone never pass HDR acceptance.
- Working wlroots **Vulkan renderer** and the exact Pi V4L2 HEVC Main10
  SAND/dmabuf import path across Chromium/Mesa/compositor.
- Source transfer function, BT.2020 colorimetry and HDR metadata/service
  signaling. Main10 alone does not prove HDR.
- Browser presentation preserving color/metadata rather than silently
  stripping it or tone-mapping to SDR.
- Correct KMS/HDMI mode, bit depth, color space/EOTF and output metadata.
- Physical display indicating real HDR; distinguish it from display effects
  and tone-mapped SDR, ideally with calibrated measurements.
- Repeat after mode changes, hotplug and reboot; SDR content stays correctly
  tagged. No unsupported HDR flags or claim may substitute for this evidence.

## Maintenance

Confirm all four browser packages stay held during OS upgrades. A coordinated
browser change requires all four digests/version checks and repeated acceptance.
An administrator's reviewed extension change must fail for corrupt/unsafe
archives, preserve root-only writes, switch atomically and allow rollback.
Close the kiosk before switching. Verify manual recovery and session loss.
Unsigned CI checksums are not a trusted release signature.
