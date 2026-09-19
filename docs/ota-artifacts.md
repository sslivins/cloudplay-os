# OTA artifact and discovery contract (schema 1)

These modules prepare **private staging directories only**. They never resolve
block devices, format partitions, activate a slot, or write boot-control. A
successful return is not permission to boot an unconfirmed candidate. The slot
writer owns the subsequent invalidation, copy, generated-file validation,
read-back verification, activation and durable-state protocol.

## Python API

```python
from pathlib import Path
from updater.artifacts import SemVer, verify_bundle, verify_tree, ArtifactError
from updater.discovery import Discovery, DiscoveryError

SemVer.parse("1.2.0-beta.11")  # comparable; build metadata does not affect precedence

metadata = verify_bundle(
    bundle, signature, keys_dir, destination,  # pathlib.Path objects
    platform="pi5", channel="beta",
    current_version="1.0.0", highest_version="1.0.0",
    minimum_key_epoch=1, data_schema=1,
)
verify_tree(destination, metadata)  # returns None or raises

client = Discovery(
    repo="sslivins/cloudplay-os", channel="beta",
    platform=None,             # specify when releases contain multiple platforms
    cache_file=None,           # production: a root-owned persistent Path under /data
)
release = client.check(current_version, force=False)  # dict or None
bundle, signature = client.download(
    release, directory,
    progress=None,             # callback(received_bytes, total_bytes), all 4 assets
    cancel=None,               # callback() -> truthy to cancel
)
```

The Python function signatures use keyword-only policy arguments after
`destination`, and keyword-only `force`. `SemVer.parse(value: str)` returns a
comparable `SemVer`; `verify_bundle(...) -> dict`;
`verify_tree(destination: Path, metadata: dict) -> None`;
`check(current_version: str, *, force=False) -> dict | None`;
`download(release: dict, directory: Path, progress=None, cancel=None)
-> tuple[Path, Path]`. The constructor also accepts a keyword-only `clock`
callable for deterministic scheduler tests (default `time.time`).

`check()` returns at least `version`, `release_id` (numeric GitHub release ID),
`notes`, `published_at`, `bundle_url`, `signature_url`, `size`, and `name`.
It also returns `tag`, `catalog_url`, `catalog_signature_url`, and `assets`
(each suffix maps to its numeric asset ID, exact URL and size). Keep that entire
dictionary server-side; do not rebuild it from launcher-provided URLs.
`download()` re-fetches the release by ID and rejects changed IDs/URLs/sizes.
All remote release metadata remains **untrusted** until artifact verification.

`verify_bundle()` returns the exact signed inner metadata dictionary described
below; it does not add a catalog or discovered-release object to that dictionary.
The caller must compare its returned version with the selected release version.
`verify_tree()` checks precisely that signed tree: generated files are **not**
silently skipped. Call it before copying device identity or materializing
slot-specific files. A later physical-slot verifier must separately validate the
closed generated set and all unchanged manifest entries.

The caller supplies durable version/key floors, platform and data schema from
trusted device configuration. An upgrade must be strictly newer than the running
version and at least the highest accepted version; the current version must meet
the bundle's minimum source version. There is no downgrade bypass in this API.
Beta permits stable and beta artifacts; stable rejects prerelease SemVers even
when GitHub incorrectly marks them as non-prereleases.

## Four immutable assets

For version `1.2.0-beta.2` and platform `pi5`:

1. `cloudplay-os-1.2.0-beta.2-pi5.tar.zst`
2. `cloudplay-os-1.2.0-beta.2-pi5.tar.zst.minisig`
3. `cloudplay-os-1.2.0-beta.2-pi5.tar.zst.catalog.json`
4. `cloudplay-os-1.2.0-beta.2-pi5.tar.zst.catalog.json.minisig`

The signed outer catalog binds `schema`, exact `name`, `version`,
`source_commit`, `platform`, `channel`, `key_epoch`, `compressed_size`,
`uncompressed_size`, compressed `sha256`, and `required_staging_bytes`.
It is emitted **after** compression. Neither compressed size nor archive size is
embedded inside the archive: there is no size/hash self-reference.

Both detached signatures must verify with the **same installed public key**
before invoking zstd. The compressed size/hash must match the signed catalog,
and its identity must match the signed inner metadata. A GitHub asset name,
digest, release date, or `immutable` UI indicator is not a trust root.

The inner tar starts with one plain regular `meta.json`, followed exclusively
by `boot/` and `root/` members. Metadata has exactly these keys:

```text
schema (1), version, source_commit (40 lowercase hexadecimal characters),
minimum_source_version, platform, channel, key_epoch,
created_at (UTC YYYY-MM-DDTHH:MM:SSZ), workflow,
data_schema_min, data_schema_max, payload_bytes,
manifest, manifest_sha256
```

`manifest` maps every directory, regular file and symlink path to numeric
`mode`, `uid`, `gid` and `type` (`directory`, `file`, `symlink`). Regular files
also have `size` and lowercase SHA-256 `sha256`; symlinks have `target`.
`payload_bytes` sums regular-file content only.
`manifest_sha256` hashes the manifest serialized as sorted-key JSON with ASCII
escapes, no whitespace, and no nonfinite values. JSON duplicate keys are rejected.
Root entries may additionally contain `xattrs`, mapping supported Linux extended
attribute names to canonical base64 values. The signed manifest binds these
values; they are not accepted from tar extension headers.

## Safe extraction and resource bounds

* Linux `minisign` and `zstd` executables must be installed. No Python third-party
  packages are used. Minisign commands time out after 60 seconds.
* zstd decompression has a 128 MiB memory/window bound, 600-second process
  deadline, and 8 GiB output limit. Compressed input is capped at 4 GiB.
* Metadata is capped at 32 MiB, catalog at 16 KiB, signatures/keys at 8 KiB,
  manifest at 200,000 entries, and paths at 4,096 characters.
* The uncompressed archive is spooled under the private destination, then
  validated before extraction. Tar extension headers are bounded before
  `tarfile` can allocate their contents. Sparse, global-PAX, xattr and unknown
  extension semantics are not accepted.
* Boot regular-file bytes must be at most 768 MiB; root bytes at most 6 GiB
  (75% of their 1 GiB/8 GiB slot budgets).
* Require an empty, non-symlink staging directory; Linux requires caller
  ownership and mode 0700. Its parent must already exist. There is no merging
  into an existing filesystem tree.
* Reject absolute member paths, traversal, backslashes, duplicate paths,
  missing directory parents, and parents declared as symlinks. FAT boot paths
  additionally reject case collisions and names FAT cannot represent.
* Ext4 root paths preserve Linux case distinctions and multiarch package
  names such as `libc6:arm64.list`. Windows extraction rejects paths that
  would alias devices, create alternate data streams, or lose trailing
  characters; real OS bundles require a POSIX staging filesystem.
* Reject devices, FIFOs, hardlinks and sparse files. Source hardlinks are
  deliberately serialized as independent regular files by the builder.
* Relative links must remain inside their own filesystem root. Standard
  absolute Unix links such as `root/bin -> /usr/bin` are preserved as **target
  root links**, never resolved against the build or updater host.
* Create symlinks only after regular files; verify using `lstat`, without
  following symlink directories. No file write can pass through a symlink.
* Linux preserves and verifies numeric UID/GID and modes, including special
  mode bits. Production requires root when source ownership differs. Windows
  tests verify content but cannot attest Linux numeric ownership/modes.
* Linux preserves and checks `security.capability`, POSIX access/default ACLs,
  and `user.*` extended attributes through the signed manifest. There are at
  most 32 attributes and 64 KiB of decoded values per entry. Unsupported
  namespaces fail explicitly instead of losing metadata. FAT entries cannot
  carry extended attributes. Ownership and modes are applied before restoring
  capabilities/ACLs, then all attributes are re-read and compared.

On extraction failure, the directory may contain **untrusted partial content**.
Discard it in its entirety; never resume by merging into it.

The closed generated paths, absent from the bundle, are:

```text
boot/cmdline.txt
boot/autoboot.txt
boot/slot-valid.json
root/etc/fstab
root/etc/machine-id
root/etc/ssh/ssh_host_{rsa,ecdsa,ed25519}_key
root/etc/ssh/ssh_host_{rsa,ecdsa,ed25519}_key.pub
```

No metadata-supplied exemptions or wildcard exemptions exist. In particular,
`config.txt`, `tryboot.txt`, and arbitrary files under `/etc` are not exemptions.

## Build and signing

Run the builder on Linux, from the same boot/root inputs used to assemble the
flash image in the same workflow. Platform identifiers must agree with device
configuration. Example:

```sh
python3 scripts/build-ota-bundle.py \
  --boot build/boot --root build/root --output build/release \
  --version 1.2.0-beta.2 --minimum-source-version 1.0.0 \
  --source-commit "$GITHUB_SHA" --platform pi5 --channel beta \
  --key-epoch 2 --workflow "$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
  --data-schema-min 1 --data-schema-max 1 \
  --secret-key "$EXTERNAL_SIGNING_KEY" --public-key keys/epoch-2-primary.pub
```

The secret key must be outside the repository. Provision it through the trusted
signing environment; the builder runs noninteractively, never stores a key in
the artifact, and never generates production keys. The committed epoch-1 public
keys support experimental builds. Production approval still requires
independently held recovery-key custody and completed hardware acceptance.
See [GitHub signing](ota-images.md#github-signing-workflow) for the protected
draft-beta workflow.

Public keys are named `epoch-N-primary.pub`, `epoch-N-recovery.pub`, or another
alphanumeric/hyphen/underscore role name. `N >= 1` is the trusted key epoch, not
an untrusted assertion inside metadata. The builder verifies fresh signatures
against the supplied public key and performs a full compressed round-trip
extraction/manifest check before returning. Existing asset filenames are never
overwritten. Publish all four assets only after the independent flash-image
manifest and layout checks pass; this builder does not mount or inspect images.

Rotate by shipping an overlap key directory in a signed release, then advancing
the durable floor and removing the revoked primary key in a later trusted
release. Keep recovery private keys separately. A compromised recovery key
requires reflash into a new trust root. Never rename the same public key to a
higher epoch to bypass a floor. Protect the installed directory from writes by
the launcher/browser identities.

## Discovery/network policy

Discovery paginates `/repos/{repo}/releases` (100 releases/page, maximum 20
pages), selecting the highest eligible SemVer, not GitHub `latest`. Reaching
the page bound is an explicit error, not an incomplete success. GitHub release
and asset IDs are checked again immediately before download.

Only HTTPS is allowed. Every redirect is manually checked before following,
with at most five redirects, no userinfo, nonstandard ports or scheme downgrade.
API requests stay on `api.github.com` under the configured release repository.
Initial asset URLs must match the repository/tag/name exactly. Asset redirects
permit only `github.com` (same release repository),
`release-assets.githubusercontent.com`, and `objects.githubusercontent.com`.
There is no generic URL-fetch interface for IPC.

API pages are capped at 2 MiB; persisted cache at 8 MiB. Streaming asset limits
are enforced independently of Content-Length. Network reads use 30-second
socket timeouts; API-body and full-download deadlines are 60/1,800 seconds.
Only identity content encoding and full HTTP 200 asset responses are accepted.
Partial downloads are **not resumed**. New attempts use a new private release
directory, start from byte zero and refuse stale `.part` files. Cancellation
removes partial/completed files created by that call only.

Checks retain per-page ETags and atomic cached results. Set `cache_file` in
production to preserve scheduling across service restarts. Normal checks are
six hours apart; explicit forced checks immediately revalidate with GitHub using
per-page ETags instead of returning cached results without contacting the server.
Failures back off from 60 seconds to six hours, including manual retries; a
manual retry during backoff reports `BACKOFF`, never stale success.
The caller/timer adds randomized delay.
`last_successful_check` (Unix timestamp or None) and `next_check_at` expose
staleness/scheduling. A failed refresh never makes partial pages look like a
successful complete scan. API outages must be handled by the service as
nonfatal to boot.

## Errors and tests

`ArtifactError` and `DiscoveryError` are `ValueError` subclasses with a stable
`.code`; the displayed text begins `CODE:`.

Artifact codes: `VERSION`, `METADATA`, `MANIFEST`, `COMPATIBILITY`,
`KEY_EPOCH`, `SIGNATURE`, `SIGNATURE_TOOL`, `CATALOG`, `LIMIT`, `DECOMPRESS`,
`ARCHIVE`, `OWNERSHIP`, `STAGING`, `SPACE`, `IO`; builder adds `BUILD` and
`IMMUTABLE`. Native filesystem/subprocess errors during building may also
terminate the CLI with exit status 1.

Discovery codes: `CONFIG`, `URL`, `REDIRECT`, `NETWORK`, `HTTP`, `API`,
`CACHE`, `LIMIT`, `TIMEOUT`, `BACKOFF`, `RELEASE`, `RELEASE_CHANGED`,
`DIRECTORY`, `IMMUTABLE`, `CANCELLED`, `TRUNCATED`, `DOWNLOAD`.

Run the existing stdlib test runner:

```text
python -m unittest discover -s tests -p "test_ota_*.py"
```

Tests cover SemVer, malicious tar members/extensions, bounded metadata,
manifest tampering, ownership records, key floors, signature-before-zstd
ordering, signed catalog consistency, symlink-parent writes, redirect chains,
pagination, cache/backoff, immutable asset identity and bounded/cancellable
downloads. Windows may skip the symlink-preservation test without symlink
privilege. Linux execution is required to attest numeric modes/ownership and
actual minisign/zstd interoperability; no physical device access is needed.
