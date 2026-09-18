# Experimental A/B images

The released preview uses a two-partition image and is not an OTA baseline.
Enabling A/B updates requires one deliberate reflash. There is no in-place
conversion of a two-partition installation.

The experimental image has a 64 MiB FAT boot-control partition, two 1 GiB FAT
boot partitions, two 8 GiB ext4 roots, and a shared ext4 data partition.
All three FAT entries precede the Linux filesystem entries in the GPT.
Firmware boot ordinals therefore agree with partition numbers: A is 2 and B
is 3. Root A and B are partitions 4 and 5. Labels are diagnostic; boot
arguments and fstab use unique partition UUIDs.

The sparse image is 20 GiB. A card must provide at least 30,000,000,000 usable
bytes; marketed 32 GB cards do not necessarily provide 32,000,000,000 bytes.
Only partition 6 grows on first boot. Fixed boot/root boundaries never move.
Initialization authenticates the saved layout against the running disk before
growth, preserves partition UUIDs, and refuses a root/boot mismatch or a data
mount from another disk.

## Persistent state

`cloudplay-data.service` completes before the network, SSH, and kiosk start.
An initialization error blocks those services rather than silently putting
new state on the root partition.

| Path below `/data/cloudplay` | Purpose |
| --- | --- |
| `layout.json` | GPT disk identity and partition UUIDs |
| `identity/machine-id` | Device-wide system identity |
| `identity/ssh/` | Per-device SSH host keys |
| `network/connections/` | NetworkManager saved connections |
| `network/onboarding/` | Network setup state |
| `profiles/A/`, `profiles/B/` | Rollback-separated provider profiles |
| `update/` | Root-owned updater state and staging |
| `update/config.json` | Persistent root-only update policy, seeded once |

The active slot's profile directory is bound onto
`/home/cloudplay/.config/cloudplay`. Both providers live inside that directory.
Network settings are shared between slots. Browser profile writes are not:
the update protocol snapshots the stopped provider before tentative boot.
Machine identity must be generated into a candidate before it is booted;
a mismatched identity fails closed rather than presenting a different device
on the network.

The boot-local deadline guard starts from the actual boot mount before data
initialization and does not load persistent configuration. An approved candidate
can therefore return to its identified last-good slot even if data preparation
stalls while systemd continues feeding the hardware watchdog. Abnormal guard or
data/bootstrap failures stop the kiosk and report local recovery through the
journal, console and an available Plymouth splash.

## Build gates

The normal `scripts/build-image.sh` path remains the non-OTA preview.
`CLOUDPLAY_OTA_EXPERIMENTAL=1` enables a separate, explicitly experimental path.
Before pi-gen runs, `image-build/preflight.py` requires:

- A clean committed checkout and a committed
  `image-build/hardware-approval.json` identifying the tested platform,
  minimum EEPROM date, exact boot order, and physical acceptance status.
- `experimental_build_approved: true` and `production_approved: false`.
- Independent committed primary and recovery public keys under
  `image-build/keys/`, named `epoch-N-primary.pub` and
  `epoch-N-recovery.pub`.
- An owner-only private signing-key file **outside the repository**.
- Explicit version, minimum source version, platform, signing epoch, and
  workflow identity through the `CLOUDPLAY_OTA_*` variables checked by
  preflight.

The public signing/recovery keys and experimental CM5 hardware policy are
versioned in this tree. Private keys are held outside the checkout and never
copied to test devices or release assets. Stable/production approval still
requires independently held recovery-key custody and completed hardware
acceptance; the checked-in policy explicitly denies production approval.

The export snapshots the exact pi-gen boot/root inputs. Both slots and the
signed bundle are derived from that snapshot; generated identity, fstab and
cmdline files are checked separately. Assembly accepts only a newly created
regular image file, verifies its loop backing file before formatting, compares
both mounted slot inventories, and emits checksums and provenance with
`production_baseline: false`. It never formats a caller-supplied physical disk.
OTA images mask automatic EEPROM updates: firmware changes are separate
maintenance and must not silently invalidate the reviewed boot/rollback policy.
FAT partitions are written and read back with `mtools`; the host needs no
vfat kernel module. Ext4 partitions are mounted only through the verified
new-image loop device. Assembly compares the inputs to the metadata returned
by the signed-bundle round trip before copying either slot.

## Validation and release status

See [artifact verification](ota-artifacts.md) and
[runtime protocol](ota-runtime.md) for the remaining contracts.
The native menu presents update status asynchronously, requires explicit
confirmation for install/restart, and does not interrupt an active streaming
service. A backend safety lock cannot be bypassed by the UI.

A signed lab round-trip or a green test suite is not physical power-loss
acceptance. The missing-control-file case must not be confused with successful
rollback: on the measured firmware, missing global `autoboot.txt` can select
the first bootable slot rather than the last-good slot. During destructive
staging, withholding the candidate's `config.txt` and `tryboot.txt` is the
firmware-visible protection.

A supported baseline additionally requires a tested browser/UI privilege
boundary, A-to-B and B-to-A full updater runs, profile/identity persistence,
confirmation failure and watchdog rollback, cold boots, and interrupted
partition/control writes. Do not label an experimental image production-ready
or close the OTA acceptance issue before those gates pass.

Gaming, audio, provider profiles, and the ordinary Main Menu retain UID 1000.
That session can only read updater status and request entry into update controls.
The root transition broker closes the gaming seat and user manager, confirms
no UID-1000 process remains, and opens a private compositor/native UI as
`cloudplay-update` (UID/GID 450) on VT8. Only this identity and root can control
the updater. There is no browser in the trusted compositor's 0700 runtime.

The provider interlock covers the streaming session. Candidate boots enter
the isolated update session automatically and supply fresh GTK and real
Wayland-roundtrip heartbeats during confirmation. Returning to gaming is
refused while an update/candidate requires completion. Factory approval flags
remain false until the particular build/device is explicitly validated;
ordinary menu or web requests cannot toggle them.
