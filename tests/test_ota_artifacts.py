"""Portable contract/security tests. No device access or signing secrets."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import uuid

from updater import artifacts as a


def manifest_entry(kind="file", data=b"kernel", **extra):
    result = dict(type=kind, mode=0o755 if kind == "directory" else 0o644,
                  uid=os.getuid() if hasattr(os, "getuid") else 0,
                  gid=os.getgid() if hasattr(os, "getgid") else 0)
    if kind == "file":
        result.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    result.update(extra)
    return result


def metadata():
    entries = {"boot": manifest_entry("directory"),
               "root": manifest_entry("directory"), "boot/kernel": manifest_entry()}
    return dict(schema=1, version="1.2.0-beta.2", source_commit="a" * 40,
                minimum_source_version="1.0.0", platform="pi5", channel="beta",
                key_epoch=2, created_at="2026-09-17T00:00:00Z", workflow="run:123",
                data_schema_min=1, data_schema_max=2, manifest=entries,
                manifest_sha256=hashlib.sha256(a.canonical_json(entries)).hexdigest(),
                payload_bytes=6)


def seal(meta):
    meta["manifest_sha256"] = hashlib.sha256(a.canonical_json(meta["manifest"])).hexdigest()
    meta["payload_bytes"] = sum(r.get("size", 0) for r in meta["manifest"].values())
    return meta


CONSTRAINTS = dict(platform="pi5", channel="beta", current_version="1.1.0",
                   highest_version="1.1.0", minimum_key_epoch=2, data_schema=1)


class SemVerTests(unittest.TestCase):
    def test_official_precedence(self):
        versions = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
                    "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                    "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        parsed = [a.SemVer.parse(v) for v in versions]
        self.assertEqual(parsed, sorted(reversed(parsed)))
        for left, right in zip(parsed, parsed[1:]):
            self.assertLess(left, right)

    def test_build_metadata_ignored(self):
        self.assertEqual(a.SemVer.parse("1.2.3+123"), a.SemVer.parse("1.2.3+abc"))
        self.assertEqual(hash(a.SemVer.parse("1.2.3+123")), hash(a.SemVer.parse("1.2.3")))

    def test_invalid_versions(self):
        for value in ("v1.2.3", "1.2", "01.2.3", "1.2.3-01", "1.2.3-", "1.2.3+",
                      "1.2.3-a..b", " 1.2.3", "1.2.3\n", "1.2.3-β", None, True):
            with self.subTest(value=value), self.assertRaises(a.ArtifactError):
                a.SemVer.parse(value)

    def test_numeric_identifier_precedence(self):
        self.assertLess(a.SemVer.parse("1.0.0-9"), a.SemVer.parse("1.0.0-10"))
        self.assertLess(a.SemVer.parse("1.0.0-999"), a.SemVer.parse("1.0.0-a"))
        self.assertGreater(a.SemVer.parse("1.0.0-a.b"), a.SemVer.parse("1.0.0-a"))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="cloudplay-ota-artifact-"))
        self.archive = self.work / "payload.tar"
        self.dest = self.work / "stage"
        self.dest.mkdir(mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.work)

    def tar(self, meta=None, extra=(), transform=None):
        meta = metadata() if meta is None else meta
        with tarfile.open(self.archive, "w", format=tarfile.PAX_FORMAT) as archive:
            blob = a.canonical_json(meta)
            info = tarfile.TarInfo("meta.json")
            info.size = len(blob)
            archive.addfile(info, io.BytesIO(blob))
            for name, record in meta["manifest"].items():
                info = tarfile.TarInfo(name)
                info.mode, info.uid, info.gid = record["mode"], record["uid"], record["gid"]
                body = b"kernel"
                if record["type"] == "directory":
                    info.type = tarfile.DIRTYPE
                elif record["type"] == "symlink":
                    info.type, info.linkname = tarfile.SYMTYPE, record["target"]
                else:
                    info.size = record["size"]
                if transform:
                    transform(info)
                archive.addfile(info, io.BytesIO(body) if info.isreg() else None)
            for info, body in extra:
                archive.addfile(info, io.BytesIO(body) if info.isreg() else None)
        return meta

    def extract(self):
        return a._extract_archive(self.archive, self.dest, CONSTRAINTS)

    def test_extract_and_verify(self):
        meta = self.tar()
        self.assertEqual(self.extract(), meta)
        a.verify_tree(self.dest, meta)
        self.assertEqual((self.dest / "boot" / "kernel").read_bytes(), b"kernel")

    def test_extract_and_rehash_report_actual_work_without_changing_manifest(self):
        meta = self.tar()
        events = []
        actual = a._extract_archive(self.archive, self.dest, CONSTRAINTS,
                                    activity=lambda *value: events.append(value))
        self.assertEqual(actual, meta)
        self.assertIn(("extract", 0, 6), events)
        self.assertIn(("extract", 6, 6), events)
        self.assertIn(("file_attributes",), events)
        self.assertIn(("archive_layout", self.archive.stat().st_size,
                       self.archive.stat().st_size), events)
        events.clear()
        a.verify_tree(self.dest, meta, activity=lambda *value: events.append(value))
        self.assertEqual(events, [("check_prepared", 0, 6), ("check_prepared", 6, 6)])
        (self.dest / "boot/kernel").write_bytes(b"broken")
        with self.assertRaisesRegex(a.ArtifactError, "MANIFEST"):
            a.verify_tree(self.dest, meta, activity=lambda *value: events.append(value))

    def test_hash_progress_counts_chunks_not_time(self):
        content = b"x" * (2 * 1024**2 + 17)
        path = self.work / "hash-input"
        path.write_bytes(content)
        chunks = []
        self.assertEqual(a.sha256_file(path, progress=chunks.append),
                         hashlib.sha256(content).hexdigest())
        self.assertEqual(chunks, [1024**2, 1024**2, 17])

    def test_tampered_file(self):
        meta = self.tar()
        self.extract()
        (self.dest / "boot" / "kernel").write_bytes(b"broken")
        with self.assertRaisesRegex(a.ArtifactError, "MANIFEST"):
            a.verify_tree(self.dest, meta)

    def test_added_or_missing_file(self):
        meta = self.tar()
        self.extract()
        rogue = self.dest / "root" / "rogue"
        rogue.write_text("bad")
        with self.assertRaises(a.ArtifactError):
            a.verify_tree(self.dest, meta)
        rogue.unlink()
        (self.dest / "boot" / "kernel").unlink()
        with self.assertRaises(a.ArtifactError):
            a.verify_tree(self.dest, meta)

    def test_metadata_constraints(self):
        updates = [{"platform": "other"}, {"key_epoch": 1}, {"key_epoch": True},
                   {"data_schema_min": 2}, {"schema": 2}, {"version": "1.0.0"},
                   {"version": "1.1.0+build"}, {"minimum_source_version": "2.0.0"},
                   {"source_commit": "branch-name"}, {"payload_bytes": 100},
                   {"manifest_sha256": "0" * 64}, {"channel": "nightly"},
                   {"extra": "field"}]
        for change in updates:
            with self.subTest(change=change), self.assertRaises(a.ArtifactError):
                a.validate_metadata(metadata() | change, **CONSTRAINTS)

    def test_high_water_and_stable_channel(self):
        for change in (dict(highest_version="2.0.0"), dict(channel="stable"),
                       dict(data_schema=3), dict(minimum_key_epoch=3)):
            with self.subTest(change=change), self.assertRaises(a.ArtifactError):
                a.validate_metadata(metadata(), **(CONSTRAINTS | change))

    def test_safe_absolute_links_validation(self):
        for link in ("/usr/bin", "../lib", "/etc/alternatives/editor"):
            a._link("root/usr/bin", link)
        for link in ("../../../host", "//host/path", "C:\\host", "file\0bad"):
            with self.subTest(link=link), self.assertRaises(a.ArtifactError):
                a._link("root/usr/bin", link)

    def test_absolute_symlink_is_preserved_not_followed(self):
        try:
            probe = self.work / "probe"
            os.symlink("/usr/bin", probe)
            probe.unlink()
        except OSError:
            self.skipTest("Windows symlink privilege unavailable")
        meta = metadata()
        meta["manifest"]["root/bin"] = manifest_entry("symlink", target="/usr/bin", mode=0o777)
        self.tar(seal(meta))
        self.extract()
        self.assertEqual(os.readlink(self.dest / "root" / "bin"), "/usr/bin")
        a.verify_tree(self.dest, meta)

    def test_symlink_parent_manifest_rejected(self):
        meta = metadata()
        meta["manifest"]["root/link"] = manifest_entry("symlink", target="/etc")
        meta["manifest"]["root/link/evil"] = manifest_entry()
        with self.assertRaisesRegex(a.ArtifactError, "parent"):
            a.validate_metadata(seal(meta), **CONSTRAINTS)

    def test_path_rejections(self):
        for name in ("../escape", "/root/x", "root/../evil", "root//x", "boot-control/x",
                     "root/./x", "root\\x", "boot/x:stream", "boot/a.",
                     "boot/a ", "root/x\nbad"):
            with self.subTest(name=name), self.assertRaises(a.ArtifactError):
                a._path(name)

    def test_real_linux_package_names_and_case_are_valid(self):
        self.assertEqual(a._path("root/var/lib/dpkg/info/libc6:arm64.list"),
                         "root/var/lib/dpkg/info/libc6:arm64.list")
        meta = metadata()
        meta["manifest"]["root/Foo"] = manifest_entry()
        meta["manifest"]["root/foo"] = manifest_entry()
        a.validate_metadata(seal(meta), **CONSTRAINTS)

    def test_windows_staging_does_not_create_ads_or_device_names(self):
        with mock.patch.object(a.os, "name", "nt"):
            for name in ("root/x:stream", "root/NUL", "root/a.", "root/a "):
                with self.subTest(name=name), self.assertRaises(a.ArtifactError):
                    a._host_path(name)

    def test_duplicate_and_traversal_tar_members(self):
        for name in ("boot/kernel", "../escape", "/root/x", "boot-control/autoboot.txt"):
            with self.subTest(name=name):
                info = tarfile.TarInfo(name)
                self.tar(extra=[(info, b"")])
                with self.assertRaises(a.ArtifactError):
                    self.extract()
                shutil.rmtree(self.dest)
                self.dest.mkdir(mode=0o700)

    def test_special_tar_members_rejected(self):
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            def transform(info):
                if info.name == "boot/kernel":
                    info.type, info.size = kind, 0
                    info.linkname = "root/etc"
            with self.subTest(kind=kind):
                self.tar(transform=transform)
                with self.assertRaisesRegex(a.ArtifactError, "forbidden"):
                    self.extract()
                shutil.rmtree(self.dest)
                self.dest.mkdir(mode=0o700)

    def test_hash_and_attributes_mismatch(self):
        for key, value in (("sha256", "0" * 64), ("mode", 0o600), ("uid", 123456)):
            meta = metadata()
            if key == "sha256":
                meta["manifest"]["boot/kernel"][key] = value
                self.tar(seal(meta))
            else:
                def transform(info):
                    if info.name == "boot/kernel":
                        setattr(info, key, value)
                self.tar(transform=transform)
            with self.subTest(key=key), self.assertRaises(a.ArtifactError):
                self.extract()
            shutil.rmtree(self.dest)
            self.dest.mkdir(mode=0o700)

    def test_manifest_generated_exemptions_closed(self):
        for name in a.GENERATED_PATHS:
            meta = metadata()
            meta["manifest"][name] = manifest_entry()
            with self.subTest(name=name), self.assertRaisesRegex(a.ArtifactError, "generated"):
                a.validate_metadata(seal(meta), **CONSTRAINTS)

    def test_case_collisions_and_slot_budget(self):
        meta = metadata()
        meta["manifest"]["boot/KERNEL"] = manifest_entry()
        with self.assertRaises(a.ArtifactError):
            a.validate_metadata(seal(meta), **CONSTRAINTS)
        meta = metadata()
        meta["manifest"]["boot/kernel"]["size"] = a.BOOT_LIMIT + 1
        with self.assertRaisesRegex(a.ArtifactError, "75%"):
            a.validate_metadata(seal(meta), **CONSTRAINTS)

    def test_duplicate_json_keys_and_nonfinite(self):
        for blob in (b'{"a":1,"a":2}', b'{"a":NaN}', b"[]", b"\xff"):
            with self.subTest(blob=blob), self.assertRaises(a.ArtifactError):
                a._json(blob, 100)

    def test_nonzero_trailing_archive_rejected(self):
        self.tar()
        with self.archive.open("ab") as output:
            output.write(b"hidden")
        with self.assertRaises(a.ArtifactError):
            self.extract()

    def test_oversized_extension_before_tarfile_allocation(self):
        info = tarfile.TarInfo("pax")
        info.type, info.size = tarfile.XHDTYPE, a.MAX_PATH * 4 + 1
        self.archive.write_bytes(info.tobuf() + b"\0" * 1024)
        with self.assertRaisesRegex(a.ArtifactError, "extension"):
            a._scan_tar(self.archive)

    def test_metadata_limit(self):
        self.tar()
        with mock.patch.object(a, "MAX_METADATA", 10), self.assertRaises(a.ArtifactError):
            self.extract()

    def signed_fixture(self):
        meta = self.tar()
        bundle = self.work / "cloudplay-os-1.2.0-beta.2-pi5.tar.zst"
        bundle.write_bytes(b"compressed fixture")
        signature = bundle.with_name(bundle.name + ".minisig")
        signature.write_bytes(b"test-only signature")
        keys = self.work / "keys"
        keys.mkdir()
        (keys / "epoch-2-primary.pub").write_text("test-only public key placeholder")
        catalog = {k: meta[k] for k in
                   ("schema", "version", "source_commit", "platform", "channel", "key_epoch")}
        catalog.update(name=bundle.name, compressed_size=bundle.stat().st_size,
                       uncompressed_size=self.archive.stat().st_size,
                       sha256=a.sha256_file(bundle), required_staging_bytes=1024**2)
        catalog_path = bundle.with_name(bundle.name + ".catalog.json")
        catalog_path.write_bytes(a.canonical_json(catalog))
        catalog_path.with_name(catalog_path.name + ".minisig").write_bytes(b"test-only signature")
        return bundle, signature, keys, catalog_path

    def test_signatures_precede_decompression(self):
        bundle, signature, keys, _ = self.signed_fixture()
        calls = []
        def verify(message, sig, key):
            calls.append(("signature", message.name))
            return True
        def decompress(source, destination, **kwargs):
            calls.append(("decompress", source.name))
            shutil.copyfile(self.archive, destination)
        with mock.patch.object(a, "_verify_signature", side_effect=verify), \
                mock.patch.object(a, "_decompress", side_effect=decompress):
            meta = a.verify_bundle(bundle, signature, keys, self.dest, **CONSTRAINTS)
        self.assertEqual([c[0] for c in calls], ["signature", "signature", "decompress"])
        self.assertEqual(meta["version"], "1.2.0-beta.2")
        self.assertFalse((self.dest / ".verified-archive.tar").exists())

    def test_bad_signature_never_decompresses(self):
        bundle, signature, keys, _ = self.signed_fixture()
        with mock.patch.object(a, "_verify_signature", return_value=False), \
                mock.patch.object(a, "_decompress") as decompress, \
                self.assertRaisesRegex(a.ArtifactError, "SIGNATURE"):
            a.verify_bundle(bundle, signature, keys, self.dest, **CONSTRAINTS)
        decompress.assert_not_called()

    def test_catalog_size_hash_epoch_bound(self):
        bundle, signature, keys, catalog_path = self.signed_fixture()
        original = json.loads(catalog_path.read_bytes())
        for change in ({"compressed_size": 1}, {"sha256": "0" * 64},
                       {"key_epoch": 3}, {"name": "other.tar.zst"}):
            catalog_path.write_bytes(a.canonical_json(original | change))
            with self.subTest(change=change), \
                    mock.patch.object(a, "_verify_signature", return_value=True), \
                    mock.patch.object(a, "_decompress") as decompress, \
                    self.assertRaises(a.ArtifactError):
                a.verify_bundle(bundle, signature, keys, self.dest, **CONSTRAINTS)
            decompress.assert_not_called()

    def test_floor_filters_keys_before_signature_tool(self):
        bundle, signature, keys, _ = self.signed_fixture()
        with mock.patch.object(a, "_verify_signature") as verify, \
                self.assertRaises(a.ArtifactError):
            a.verify_bundle(bundle, signature, keys, self.dest, **(CONSTRAINTS | {"minimum_key_epoch": 3}))
        verify.assert_not_called()

    def test_nonempty_staging_refused(self):
        (self.dest / "existing").write_text("preserve")
        with self.assertRaisesRegex(a.ArtifactError, "STAGING"):
            a.verify_bundle(self.work / "absent", self.work / "absent", self.work,
                            self.dest, **CONSTRAINTS)
        self.assertEqual((self.dest / "existing").read_text(), "preserve")

    def test_mount_root_is_not_a_staging_directory(self):
        with mock.patch.object(Path, "is_mount", return_value=True), \
                self.assertRaisesRegex(a.ArtifactError, "STAGING"):
            a._private_directory(self.dest)

    def test_minisign_timeout_and_command(self):
        with mock.patch.object(a.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(a._verify_signature(Path("bundle"), Path("sig"), Path("pub")))
            self.assertEqual(run.call_args.args[0], ["minisign", "-Vm", "bundle", "-x", "sig", "-p", "pub"])
            self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_zstd_memory_flag_and_expansion_limit(self):
        process = mock.Mock()
        process.stdout = io.BytesIO(b"x" * 100)
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(a.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(a, "MAX_EXPANDED", 50), \
                self.assertRaisesRegex(a.ArtifactError, "LIMIT"):
            a._decompress(self.work / "bundle", self.work / "expanded")
        self.assertIn("--memory=128MB", popen.call_args.args[0])
        process.kill.assert_called_once()

    def test_gnu_sparse_and_chained_pax_rejected_before_parser(self):
        header = tarfile.TarInfo("sparse")
        header.type = tarfile.GNUTYPE_SPARSE
        self.archive.write_bytes(header.tobuf() + b"\0" * 1024)
        with self.assertRaises(a.ArtifactError):
            a._scan_tar(self.archive)
        header.type = tarfile.XHDTYPE
        self.archive.write_bytes(header.tobuf() * 3 + b"\0" * 1024)
        with self.assertRaises(a.ArtifactError):
            a._scan_tar(self.archive)

    def test_declared_space_and_inner_outer_identity(self):
        bundle, signature, keys, catalog_path = self.signed_fixture()
        original = json.loads(catalog_path.read_bytes())
        for change in ({"required_staging_bytes": 1}, {"version": "9.0.0"},
                       {"uncompressed_size": self.archive.stat().st_size + 512}):
            catalog_path.write_bytes(a.canonical_json(original | change))
            with self.subTest(change=change), \
                    mock.patch.object(a, "_verify_signature", return_value=True), \
                    mock.patch.object(a, "_decompress", side_effect=lambda src, dst, **kw: shutil.copyfile(self.archive, dst)), \
                    self.assertRaises(a.ArtifactError):
                a.verify_bundle(bundle, signature, keys, self.dest, **CONSTRAINTS)
            shutil.rmtree(self.dest)
            self.dest.mkdir(mode=0o700)

    def test_builder_inventory_and_tar_roundtrip(self):
        spec = importlib.util.spec_from_file_location(
            "ota_builder", Path(__file__).parents[1] / "scripts" / "build-ota-bundle.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        boot, root = self.work / "input-boot", self.work / "input-root"
        boot.mkdir()
        root.mkdir()
        (boot / "kernel").write_bytes(b"kernel")
        (boot / "cmdline.txt").write_text("slot generated")
        entries, sources = builder.inventory(boot, root)
        self.assertNotIn("boot/cmdline.txt", entries)
        meta = metadata()
        meta["manifest"] = entries
        builder.write_tar(self.archive, seal(meta), sources)
        self.extract()
        a.verify_tree(self.dest, meta)


if __name__ == "__main__":
    unittest.main()
