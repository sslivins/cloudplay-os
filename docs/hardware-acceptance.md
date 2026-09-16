# Mandatory physical acceptance — no results claimed yet

Record image SHA256, source commit, `/usr/local/share/cloudplay/build-manifest.json`,
four installed Chromium versions, extension `cloudplay-source.json`, kernel,
Mesa/labwc versions, Pi 5 RAM/firmware, power/cooling, input devices, cable,
HDMI port, display model/firmware/EDID and exact GFN account tier/game/region.
Redact accounts, tokens and public IPs. Mark each item **not run/pass/fail** with
timestamp and evidence. All items below are currently **not run for this image**.

## Boot, owner and escape

- Flash a spare card; verify SHA256. First boot without Imager customization:
  owner wizard appears unobscured, creates owner-chosen credentials, connects
  Wi-Fi with chosen regulatory country, completes and reboots into owner GFN.
- Repeat with supported Imager customization and with Ethernet/offline first
  boot. Confirm cloud-init and piwiz cooperate rather than losing networking
  or launching a privileged browser. No wizard-account browser may run.
- Owner is nonroot; normal sudo needs the selected password; root locked;
  SSH remains disabled, no preinstalled common credentials or debug listener.
- F11 and Alt+F4 escape to desktop; panel Wi-Fi/Bluetooth/display/audio controls
  work. Settings toggles next-login autostart. Local VT recovery works.
- Unplug network, change display mode, hotplug monitor, reboot after hard
  shutdown and simulate browser crash. No respawn loop traps the owner.
- Profile mode 0700, persistent login after reboot, sign-out/profile deletion
  works. Physical-access exposure of auto-login is explicitly accepted.

## Browser, extension and genuine capability

- `chrome://version` shows the intended normal-user executable and arguments;
  `chrome://sandbox` confirms sandbox operation; no `--no-sandbox` or debugger.
- `chrome://extensions` confirms unpacked MV3 extension loaded from the
  root-owned `/opt/gfn-pi-compat` and correct reviewed version. Establish whether
  this Chromium build still honors `--load-extension`; failure blocks preview.
- Establish real HEVC/Main10 MediaCapabilities/decoder support, and observe the
  extension's behavior both with HEVC available and deliberately unavailable.
  Earlier `h265=1` experimentation enabled a real capability check; it did not
  prove that every client or display supports the resulting stream.
- GFN, NVIDIA login, optional Google login and redirect/popup/2FA flows work
  without root, remote debugging, certificate bypasses or spoofed entitlement.
  An older root diagnostic had a Google rejection of unknown cause; this is
  not evidence it is fixed, nor justification for bypassing provider controls.
- Account tier, region, browser/platform support and game genuinely authorize
  requested 4K60/HDR. A capability override cannot create service entitlement.

## Separate test matrix

1. **1080p60 SDR baseline**: prior evidence is only a starting hypothesis.
   Prove negotiated dimensions/rate/codec and active hardware decode here.
2. **3840×2160 at 60 fps SDR**: actual inbound decoded dimensions and measured
   frame rate, not merely a 4K desktop or GFN menu option. Record network bitrate,
   dropped frames, decode latency, CPU/GPU use and temperature over 30+ minutes.
3. **3840×2160 at 60 fps HDR**: all of the above plus the complete HDR proof
   below. SDR success does not pass this row. If service or presentation stack
   cannot supply HDR, record the blocker; do not declare tone-mapped SDR HDR.

Use local `chrome://webrtc-internals`, `chrome://gpu`, `chrome://media-internals`
and GFN diagnostics as applicable, without opening a remote-debug port. Capture
real codec profile/bit depth and hardware decoder evidence (V4L2 path, not
software decode inferred from a low average CPU). Test audio sync, keyboard,
mouse pointer lock, wired/Bluetooth controller latency and disconnect/reconnect.

## HDR evidence (all required)

- Run `cloudplay-hdr-check` and retain its output. For upstream labwc's HDR10
  path require labwc >=0.20.0, linked wlroots >=0.20.1, and a working **Vulkan
  renderer**. Confirm the running session uses the diagnosed binary and renderer.
  Stock Pi packages may still be on 0.9.x; upstream availability is not proof
  of installed support. A version-floor pass is not an HDR acceptance pass.
- Prove the Pi **V4L2 HEVC Main10 SAND/dmabuf import path** works with the actual
  Chromium/Mesa/Vulkan/compositor combination, not only a different renderer.
- Source stream is actually HDR: Main10 alone is insufficient. Record signaled
  transfer function (PQ or HLG as applicable), BT.2020 primaries/colorimetry and
  HDR metadata where available, plus GFN's actual HDR session indication.
- Browser imports/presents those decoded frames without stripping metadata or
  silently tone-mapping to an SDR surface.
- Kernel/KMS/compositor output selects a valid display-supported 4K60 mode with
  appropriate bit depth and color space. Capture output/connector HDR metadata,
  EOTF and link configuration using available on-device DRM/compositor tools.
- The physical display's information screen indicates the expected HDR mode;
  use known HDR highlights/black levels and, where possible, calibrated
  measurement to distinguish real HDR from SDR tone-mapping or display effects.
- Repeat after hotplug, mode change, resume/reconnect and reboot. Confirm SDR
  content is not incorrectly tagged as HDR. Document actual panel/link limits.

If the stock labwc/Chromium path lacks HDR presentation, report **HDR blocked**
and identify the missing link. A future compositor/backend change must preserve
onboarding, networking and local escape. 4K60 and HDR remain release goals, not
marketing claims until this matrix is passed.

## Maintenance/recovery

Close Chromium, perform a reviewed local extension update, confirm root-only
write permissions and the atomic link replacement, restart and verify behavior,
then roll back to a retained version. Corrupt/wrong-root/linked archives must
fail. Verify ordinary apt upgrades preserve all four held Chromium versions,
then test a coordinated browser replacement separately. Validate manual reflash
and account/session loss handling. No signed/public release is authorized by
passing a small unit test suite alone.
