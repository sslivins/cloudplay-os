# Experimental OTA runtime (schema 1)

The new `updater.state`, `updater.platform`, `updater.runtime`, `updater.boot`,
`updater.service`, and `updater.client` modules implement the privileged runtime.
They reuse the existing artifact and discovery contracts; they do not change
the image builder, launcher, installed units, or signing policy.

**This is not a production enablement.** Status remains available with mutation
disabled. Install, physical writes, pointer updates, candidate promotion,
rollback and reboot require explicit root-owned experimental hardware approval
AND verified launcher/browser isolation. No code automatically enables either
gate. Real power-cut/torn-write acceptance remains outstanding.

## Immediate parent integration contract

* Add **exactly `boot/autoboot.txt`** to the closed generated-file set in
  `updater/artifacts.py` and omit it from bundles. Slot mirrors depend on local
  last-good state; they cannot be immutable signed payload files. Runtime
  reports `ARTIFACT_CONTRACT` and refuses install/pointer changes until this
  contract exists. Existing artifact/discovery files were not edited by this
  runtime implementation.
* Boot FAT manifest entries must have UID/GID 0, directories 0755 and regular
  files 0644. FAT cannot preserve arbitrary POSIX ownership/modes or symlinks.
  Root entries retain signed numeric UID/GID, mode bits and symlink text.
  Their signed optional xattrs (including POSIX ACLs and file capabilities)
  are restored by the shared artifact helper and checked exactly by
  `artifacts.verify_attributes` during physical readback. Generated root
  files also use that setter to remove unsigned inherited ACLs/xattrs.
  A signed regular `boot/config.txt` is required; a separate `tryboot.txt` is
  rejected. Generated files require real, signed directory parents.
* Seed `/data/cloudplay/profiles/A` and `B`; bind **the physically running
  slot's** profile directory at `/home/cloudplay/.config/cloudplay` before
  launcher/provider startup, including fallback boots with DT tryboot zero.
  The profile owner must equal `browser_uid`, not automatically UID 1000.
  Current firstboot code using owner 1000 must be reconciled with the separate
  browser identity before enabling mutation.
* The trusted provider launcher must hold a **shared flock for the provider's
  entire lifetime** on `/run/cloudplay-updater/provider.lock` (beside the Unix
  socket, not inside the private 0700 journal directory). Runtime holds
  an exclusive nonblocking flock through install/restart. The file is created
  root-owned 0644 by the daemon before it listens; start the daemon before any provider launch. A
  launcher obtains its shared lock before checking `provider_launch_allowed`,
  and refuses launch when false. Otherwise there is a start-vs-install race.
  Browser processes must not independently start privileged provider units.
  Runtime additionally refuses any running browser-UID process or active
  system `cloudplay-provider-*`/`cloudplay-stream.service` unit. The existing
  launcher uses a **user** `cloudplay-stream.service`; its lifetime must be
  covered by the trusted shared-lock holder and the isolated browser UID.
* Bake `/usr/share/cloudplay/release.json` as an ordinary signed file containing
  exact `version`, `source_commit`, and `launcher_smoke_passed: true`.
  **Do not embed the enclosing manifest digest in that signed file**: that
  would be a hash self-reference. Health hashes the release record against its
  signed manifest entry and checks the digest in generated `slot-valid.json`
  against the durable candidate manifest instead.
* Bake `/usr/share/cloudplay/kernel-command-line.txt` as an ordinary signed
  template from the image's boot arguments. Runtime verifies that template's
  signed hash and replaces only slot/root and firstboot-resize arguments.
  Console/Plymouth and other OS boot options therefore survive an OTA.
* Launcher/compositor publish bounded JSON heartbeat files at
  `/run/cloudplay/launcher-heartbeat.json` and
  `/run/cloudplay/compositor-heartbeat.json`:
  `{"boot_id":"<kernel boot UUID>","monotonic":123.0}`. They must update more
  often than every 30 seconds, including with a disconnected display.
  Their identities and parent directories must prevent browser forgery.
* Firstboot remains responsible for persistent NetworkManager connections,
  onboarding state and SSH identity binds before networking starts. Runtime
  generates machine-id from `/data/cloudplay/identity/machine-id` and SSH keys
  from `/data/cloudplay/identity/ssh`, only after verifying these root-controlled
  sources agree with the active `/etc` identity. It does not import identity
  from an update bundle. No forward data migration is performed before promotion.
* Runtime generates PARTUUID-based fstab/cmdline from the actual mounted
  root/boot/data disk. Initial image/firstboot must do likewise; labels alone
  are ambiguous when an SD and NVMe recovery image coexist. Generated fstab
  lists root/boot only: the parent's `cloudplay-data.service` owns same-disk
  `/data` mounting/growth and per-slot profile/network/identity binds on every
  boot. Putting these binds in fstab would race or deadlock that service's
  `After=local-fs.target` ordering. Runtime owns profile snapshot preparation
  and generated identity before activation; firstboot selects the prepared
  profile using the actually mounted root slot, and health verifies its mapping.

## CLI and client API

```text
python3 -m updater.service --help
python3 -m updater.service --config /data/cloudplay/update/config.json serve
python3 -m updater.service --config /data/cloudplay/update/config.json initialize
python3 -m updater.service --config /data/cloudplay/update/config.json reconcile
python3 -m updater.service --config /data/cloudplay/update/config.json health
python3 -m updater.service --config /data/cloudplay/update/config.json deadline
python3 -m updater.service --config /data/cloudplay/update/config.json shutdown
python3 -m updater.service --config /data/cloudplay/update/config.json retry
python3 -m updater.service --config /data/cloudplay/update/config.json rollback
python3 -m updater.service early-guard
python3 -m updater.service client status
```

`initialize` requires an already mounted/validated A/B image and matching
initial release record/slot sentinel. It atomically creates the initial journal;
it never infers or overwrites an existing journal. This root-only journal
provisioning does not write partitions or require enabling the hardware gate;
it permits discovery/status on a correctly provisioned but mutation-disabled
image. `retry` clears strikes only
on the reconciled last-good slot, without lowering version/key floors.
`rollback` applies only to a running durable candidate; it is not an arbitrary
reboot or downgrade primitive. No force-promotion bypass is exposed.

```python
from updater.client import Client
from updater.state import UpdateError

client = Client()  # default /run/cloudplay-updater/control.sock, 5 s deadline
status = client.status()
status = client.check()
status = client.install()
status = client.cancel()
status = client.restart()
```

These convenience methods return the status dictionary or raise `UpdateError`
with `.code`. Module-level `updater.client.request(command)` also returns the
status dictionary, for the parent's native launcher adapter.
`Client.request(command)` instead returns the wire envelope:
`{"ok":true,"status":{...}}` or
`{"ok":false,"error":{"code":"...","message":"..."}}`.
Check/install/restart acknowledge **accepted asynchronous work**, not completion.
Poll status; only `promoted` means success. No custom URLs, release versions,
paths, flags or shell arguments cross IPC. An accepted request can still fail
prechecks; its typed failure is persisted and logged.

The parent UI must map `ready_to_restart` (not `ready`),
`tryboot_running` (not `confirming`), and the staging phases documented here.
Progress is `{received,total}`, not a percent scalar. Use `can_restart` for the
restart action, not `install_enabled`. `dismiss` is not an IPC command; notices
may be dismissed in the presentation layer without a new root mutation.

Wire request: one UTF-8 JSON line with exactly `{"command":"status"}` (or
`check`, `install`, `cancel`, `restart`). Maximum request is 1024 bytes;
response is 65536 bytes. The daemon imposes a 3-second total request deadline,
four simultaneous clients, one operation worker, and at least one second
between state-changing requests from each authorized identity. Discovery adds
its own 30-minute forced-check limit and persistent backoff.

The daemon authenticates the kernel's Linux `SO_PEERCRED`, not a UID claimed in
JSON. Only root and `launcher_uid` are allowed. The socket directory is
0750 root:`socket_gid`; the socket is 0660 root:`socket_gid`. There are no
TCP listeners. Different processes with the same UID are **not** isolated.
The boolean assertion is an operator gate, not an implementation of AppArmor
or a substitute for verifying the installed UID/service policy.

Status includes `phase`, current/highest/available/candidate versions, bounded
notes, publication date, download size, received/total progress, typed `error`,
`notice`, `gate`, strikes, `can_cancel`, `can_restart`,
`provider_launch_allowed`, `install_enabled`, and discovery freshness. Raw
server-side release URLs and signed manifests are not returned to the UI.
The daemon also atomically publishes a root-owned `status.json` beside its
socket. An absent journal yields `uninitialized`; it is never silently reset.

## Root-owned JSON configuration

Firstboot should provision `/data/cloudplay/update/config.json` as root:root
0600 and use that explicit `--config` path in **every** installed root unit.
The state directory is root:root 0700. Seed configuration only when absent;
never replace an existing operator policy with baked defaults on later boots.
This keeps the same reviewed policy available across both slots and rollback.
A slot-local `/etc/cloudplay/updater.json` remains the CLI default for explicit
operator use, but must not accidentally replace persistent policy during a
candidate boot: default-false gates would prevent health/rollback execution.

Missing fields use the values below. Unknown fields are errors. Configuration,
key directory, state directory and their ancestors must not be symlinks or
non-root writable. Paths must be absolute and contain no whitespace.

```json
{
  "schema": 1,
  "experimental_hardware_validation": false,
  "hardware_evidence": "",
  "launcher_isolation_verified": false,
  "isolation_evidence": "",
  "launcher_uid": 1000,
  "browser_uid": 1001,
  "socket_gid": 1000,
  "socket_path": "/run/cloudplay-updater/control.sock",
  "state_dir": "/data/cloudplay/update",
  "keys_dir": "/usr/share/cloudplay/update-keys",
  "release_file": "/usr/share/cloudplay/release.json",
  "profile_root": "/data/cloudplay/profiles",
  "profile_mount": "/home/cloudplay/.config/cloudplay",
  "channel": "beta",
  "platform": "cm5",
  "repo": "sslivins/cloudplay-os",
  "data_schema": 1,
  "minimum_key_epoch": 1,
  "minimum_eeprom": "",
  "boot_order": "",
  "stabilization_seconds": 120,
  "deadline_seconds": 600,
  "strike_limit": 3
}
```

`minimum_eeprom` is a reviewed YYYY-MM-DD date; `boot_order` is the exact
reviewed `0x...` value. Neither is guessed or populated from a probe.
Pi114 evidence (CM5 Lite, EEPROM 2026-02-23, SD 31,914,983,424 bytes) is not
general approval for every Pi5/CM5 or media combination. Browser/provider
interlock, ownership and confinement must all be covered by `isolation_evidence`.
Only an explicitly authorized local root operator may enable these flags.

## State, staging and recovery ordering

`state.json` is bounded strict JSON with duplicate-key/nonfinite rejection,
full signed metadata, selected release identity, pending slot/generated
records, current/highest version, key floor, last-good identity, strikes, and
candidate boot/deadline state. Writes use a private temporary file, file fsync,
replace, and parent-directory fsync. `operation.lock` uses nonblocking flock;
portable tests use the corresponding Windows file lock. Status reads do not
take the operation lock. No second updater/helper can stage concurrently.

The signed version must equal the selected GitHub release version **byte for
byte**, including build metadata, beyond the artifact module's SemVer ordering
and current/highest/key/schema checks. Highest accepted version/key floors
advance durably before invalidation and survive failure or rollback. Partial
downloads use a fresh private attempt directory and never resume by merging.
Cancellation can be requested during download/verification; artifact verification
may finish its bounded subprocess before the cancellation is observed.
Cancellation is serialized against the first invalidation and refused afterward.

Physical layout validation derives disk ancestry from the actual mounted `/`,
`/boot/firmware` and `/data` major:minor identities, verifies sysfs partition
numbers, the same physical parent disk, six-entry GPT geometry, types, names,
unique PARTUUIDs and kernel byte sizes. Supported entries are exactly:

| Number | Role | Start MiB | Size MiB |
|---|---|---:|---:|
| 1 | control, FAT/basic data | 4 | 64 |
| 2 | boot A, FAT/basic data | 68 | 1024 |
| 3 | boot B, FAT/basic data | 1092 | 1024 |
| 4 | root A, Linux filesystem | 2116 | 8192 |
| 5 | root B, Linux filesystem | 10308 | 8192 |
| 6 | data, Linux filesystem | 18500 | remainder, at least 1024 |

Runtime also requires the root-controlled, bounded
`/data/cloudplay/layout.json` written by the image builder, with exactly
`{"schema":1,"disk_id":"<GPT id>","partitions":{"<partition name>":"<PARTUUID>"}}`.
It compares the GPT disk ID and all six name-to-PARTUUID entries against the
same actual mounted disk table; a foreign, incomplete or changed record is a
typed `LAYOUT_RECORD` failure. Data partition growth does not change this
identity record or the fixed slot geometry.

Targets with mounts, swap usage or block-device holders are refused.
Only the inactive boot/root filesystems are formatted. Control is never
formatted. The daemon verifies system time, reviewed EEPROM/BOOT_ORDER and
throttling status before download/staging.

1. Acquire the provider interlock, refuse running browsers, verify signatures
   and source tree, and durably record the invalid candidate.
2. Unlink target `config.txt`, `tryboot.txt`, and slot sentinel. Flush and verify
   both firmware entrypoints are absent **before changing target root**.
3. Copy inactive profiles from the stopped active profiles using `cp -a`
   (numeric IDs, xattrs and links), flush, then publish the per-slot copy.
   Reject special files and escaping profile links. A partial `.new` snapshot
   is a visible operator-cleanup error, never an implicit reuse.
4. Under a logind shutdown/sleep inhibitor, recreate inactive FAT and ext4;
   copy directories/files first, symlinks last. Preserve signed root modes and
   numeric ownership using the artifact helper. Never traverse a payload
   symlink to write a generated file. Absolute root links retain their text
   and cannot redirect extraction into host paths.
5. Generate the closed identity/fstab/cmdline/sentinel/mirror set. Check every
   manifest member and exact generated byte hash; reject extra files. Flush
   both filesystems. Durably record `publishing` with full candidate identity.
6. Publish target `config.txt` **last**, flush, verify again, unmount, then
   durably record `ready_to_restart`. No pointer changes occur during staging.
7. Restart re-verifies the inactive slot and last-good identity, writes both
   mirrors then control with flush/readback, records a tryboot attempt and
   requests `reboot "0 tryboot"`.

On boot, `reconcile` must precede launcher/provider startup. A candidate is
identified by durable pending identity even if DT tryboot is zero. A wrong or
unknown slot is never silently adopted. Old-slot boot before an attempted
restart preserves `ready_to_restart`; old-slot boot after an attempt records
rollback. An interrupted publish must pass a full inactive-slot readback.
Incomplete staging is not resumed by merging; it is reported and retried as a
new download. Per-slot profiles make old-slot fallback select the unmodified
old profile, not a post-promotion snapshot.

Health verifies active disk/slot, signed release-record hash and identity,
config hash, slot sentinel, writable data, correct per-slot profile mount and
ownership, three appliance services, and fresh launcher/compositor heartbeats.
Internet and display presence are not required. It requires 120 continuous
healthy seconds within a 600-second candidate deadline; a failed check resets
the stabilization interval, as does a gap exceeding 30 seconds between health
samples. Deadline state is boot-bound and not extended by
repeated reconciliation. Promotion writes `[all]` and `[tryboot]` in mirrors,
then authoritative control, before committing the new current/last-good state.
Forward data migrations require separate post-promotion integration.

Failed health/deadline or explicit root rollback verifies last-good, durably
records a single rollback attempt, removes the running candidate's firmware
entrypoints, writes an explicit last-good pointer, and requests normal reboot.
If firmware nevertheless returns to the candidate, runtime enters
`recovery_required` and refuses another reboot. This prevents repeatedly
looping through a missing/corrupt control pointer. A failed last-good validation
requires local recovery rather than a guessed boot selection.
`shutdown` marks an explicit graceful candidate shutdown so later old-slot
fallback does not consume a strike; daemon SIGTERM is not treated as proof of
system shutdown.

## Parent systemd wiring and external prerequisites

No installed unit is supplied/changed by this runtime-owned work. Parent must:

* Start `python3 -m updater.service early-guard` as root **after the actual
  `/boot/firmware` mount, before `cloudplay-data.service` starts**, independently
  of data, NetworkManager, greetd, updater daemon, or successful reconciliation.
  Use `Type=simple`, `DefaultDependencies=no`, `After=boot-firmware.mount`,
  `Requires=boot-firmware.mount`, and `Before=cloudplay-data.service
  sysinit.target`. Enable it from the early boot transaction, not only from
  a data-dependent updater target. It is long-running for a pending candidate;
  do not use `Type=oneshot` with ordering that waits for its deadline before
  starting data. Its CLI deliberately does not load `/data` configuration.
  An abnormal exit must surface local recovery rather than silently disabling
  this protection. It may be restarted after an unexpected process crash; its
  monotonic deadline is since boot and cannot be extended by service restart.
* Start persistent-data/identity/profile preparation and `reconcile` before
  launcher/providers; never permit replacement writes beneath an unmounted
  `/data`. A failed preparation must display local recovery.
* Run `serve` as root with the appropriate supplementary socket group policy.
  Preserve write access only to data, explicit block devices, private mount
  points, and firmware/control partitions needed by this implementation.
  Configure mount namespaces consistently: helpers must see the same actual
  root/boot/data/profile mounts. Do not permanently mount control or inactive
  slot partitions: the writer deliberately refuses already-mounted targets.
* Run `health` and `deadline` on independent short-period timers (for example
  every 10 seconds). The daemon also checks deadlines when idle. Configure
  service startup ordering so deadline reconciliation happens on every boot.
  These data-dependent helpers do not replace `early-guard`, which handles
  stalled/failed firstboot or unavailable data while systemd still feeds its
  hardware watchdog. Do not depend exclusively on a timer inside a blocked
  updater process.
* Install the reviewed Pi hardware-watchdog policy in **both** slots:
  tested kernel timeout 300 and systemd `RuntimeWatchdogSec=30s` are evidence,
  not automatic configuration here. Kernel panic/userspace hangs need this
  independent watchdog; Python cannot recover a dead kernel.
* Wire `shutdown` only for genuine system shutdown during candidate validation.
  Allow the updater's bounded staging operation to finish under its inhibitor;
  do not indiscriminately SIGKILL the writer at ordinary service stop.
* Keep source identity and NetworkManager keyfiles root-owned with correct
  private-key modes, prepare bind destinations, disable unattended package
  upgrades, and provision independent primary/recovery public keys.

Runtime dependencies are Python stdlib plus existing `minisign`/`zstd`;
physical integration also needs normal appliance utilities: `findmnt`,
`sfdisk`, `blkid`, `blockdev`, `mount`, `umount`, `mkfs.vfat`, `mkfs.ext4`,
`sync`, `cp`, `rm`, `pgrep`, `systemctl`, `systemd-inhibit`, `timedatectl`,
`vcgencmd`, and `reboot`. Commands have explicit deadlines (normally 30-120
seconds; root formatting 300; profile copy 600). Staging has a 1500-second
checkpoint deadline within an 1800-second inhibitor process. Network and
artifact bounds are documented in `ota-artifacts.md`.

### Independent boot-local deadline capability

Only an install that already passed both explicit root configuration gates can
arm the `guard` object in generated `boot/slot-valid.json`. It contains a
bounded copy of the exact GPT layout identity, last-good slot/version/manifest
and config hash, boot deadline, separate launcher/browser UIDs, true approval
flags and digests of the operator's approval evidence. It is root-controlled
boot-local configuration, not a browser-supplied token and not a new general
mutation bypass. It authorizes only this candidate's one-shot emergency return
to its identified last-good slot if persistent configuration becomes unavailable.
The normal config flags remain false by default; no baseline guard is armed
automatically. Physical-media tampering is outside the unsigned-boot threat
model, just as it is for the root configuration and kernel.

The early guard reads no `/data` files or graphical-service state. At the
configured boot-monotonic deadline (600 seconds by default), it validates actual
mounted root/boot ancestry and all six GPT partitions against that approved
record, verifies last-good, durably marks the local attempt, removes/flushes
the current candidate's config/tryboot entrypoints, writes verified last-good
pointers, then requests reboot. Missing global control therefore cannot simply
send it through the same still-bootable candidate. A repeated local attempt,
corrupt ticket, invalid last-good, or failed durable write is a visible recovery
failure, not a guessed reboot loop.

Normal rollback/promotion and the early guard share a root-only lock beneath
`/run/cloudplay-early`. Promotion disarms the guard only after health succeeds
and boot pointers have been written/read back. If the subsequent data-journal
commit is interrupted, the confirmed boot marker allows reconciliation to
finish that commit without reclassifying the confirmed slot as an unconfirmed
reboot. A boot-local emergency attempt also blocks another data-driven attempt.
All helpers use the same boot-relative deadline; slow firstboot cannot reset it.
An overlong lock holder fails visibly after a bounded additional 120 seconds.

This closes the ordinary firstboot/greetd-stall gap; it cannot promise recovery
from an unresponsive kernel, unwritable boot media, or uninterruptible storage
IO. The independent hardware watchdog and physical fault-injection acceptance
remain mandatory; the hardware gate stays disabled until explicitly approved.

## Remaining release gates and deliberate limits

* Actual cold-power/torn-write tests at every state transition remain unproven.
  FAT rename/fsync/readback is **not** claimed to be torn-write atomic.
* Missing global control demonstrably boots first bootable A despite B mirrors.
  Mirrors are diagnostics, not recovery guarantees. Withholding config is the
  required firmware gate; a sentinel alone is not one.
* Launcher/browser separation, provider flock enforcement, profile ownership,
  heartbeat trust, systemd ordering, persistent network binds and the artifact
  mirror exemption must be installed/tested by the parent before enablement.
* During inactive-slot overwrite there is only one intact OS. Independent
  loss of the running slot still requires reflash.
* Both-slot profile copy/rollback and real provider sign-ins require device
  acceptance. Low space or unsafe live Chromium links produce explicit errors.
  Persistent profiles/cookies and Wi-Fi credentials are unencrypted in v1.
* No downgrade bypass, forced health-skipping promotion, generic root IPC,
  arbitrary on-device paths, EEPROM update, or in-place repartition is exposed.

## Targeted tests

Portable and Linux-injected stdlib suites:

```text
python -m unittest discover -s tests -p test_ota_runtime.py
python -m unittest discover -s tests -p test_ota_platform.py
python -m unittest discover -s tests -p test_ota_service.py
python -m unittest discover -s tests -p test_ota_boot.py
python -m updater.service --help
```

Linux tests use real Unix peer credentials, symlinks and numeric metadata.
The two physical-stager ordering fixtures require Linux UID 0 **only for
temporary-file metadata**; every block operation/mount/reboot is injected and
no physical device is accessed. Windows skips unsupported symlink/Unix tests.
Tests cover firmware gate order, config-last publication, injected interruption,
unchanged active targets, durable floors, exact version identity, cancellation,
reconciliation with no tryboot indication, deadline/rollback loop bounds,
continuous health stabilization, IPC bounds/authentication and corrupt journals.
