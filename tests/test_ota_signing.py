"""Real minisign/zstd interoperability on a POSIX staging filesystem."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from updater import artifacts

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ota_signing_builder", ROOT / "scripts/build-ota-bundle.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@unittest.skipUnless(os.name == "posix" and shutil.which("minisign") and shutil.which("zstd"),
                     "requires POSIX, minisign and zstd")
class SigningTests(unittest.TestCase):
    def test_extended_metadata_is_in_the_signed_inventory(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-xattr-") as directory:
            work = Path(directory)
            boot, root = work / "boot", work / "root"
            boot.mkdir()
            root.mkdir()
            binary = root / "example"
            binary.write_bytes(b"payload")
            os.setxattr(binary, "user.cloudplay-test", b"preserve-required")
            manifest, _ = builder.inventory(boot, root)
            self.assertEqual(manifest["root/example"]["xattrs"], artifacts.read_xattrs(binary))
            self.assertIn("user.cloudplay-test", manifest["root/example"]["xattrs"])

    def test_real_signed_roundtrip_tamper_rejection_and_immutability(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-signing-") as directory:
            work = Path(directory)
            boot, root, keys = (work / name for name in ("boot", "root", "keys"))
            for path in (boot, root, keys):
                path.mkdir()
            secret, public = work / "signing.key", keys / "epoch-1-lab.pub"
            subprocess.run(["minisign", "-G", "-W", "-p", str(public), "-s", str(secret)],
                           check=True, timeout=30, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
            os.chmod(secret, 0o600)
            (boot / "config.txt").write_text("arm_64bit=1\n")
            (boot / "cmdline.txt").write_text("generated separately\n")
            (root / "package:arm64.list").write_text("linux package")
            (root / "Case").write_bytes(b"upper")
            (root / "case").write_bytes(b"lower")
            (root / "bin").mkdir()
            (root / "bin/example").write_bytes(b"executable")
            os.chmod(root / "bin/example", 0o751)
            os.setxattr(root / "bin/example", "user.cloudplay-test", b"metadata")
            (root / "absolute-link").symlink_to("/bin/example")
            if os.geteuid() == 0:
                os.chown(root / "case", 1000, 1000)
            args = SimpleNamespace(
                boot=boot, root=root, output=work / "release", secret_key=secret,
                public_key=public, key_epoch=1, version="0.1.0-beta.4",
                minimum_source_version="0.1.0-beta.3", source_commit="a" * 40,
                platform="cm5", channel="beta", workflow="local-integration-test",
                created_at="2026-09-19T00:00:00Z", data_schema_min=1, data_schema_max=1)
            catalog, built_metadata = builder.build(args)
            bundle = args.output / catalog["name"]
            signature = Path(str(bundle) + ".minisig")
            constraints = dict(platform="cm5", channel="beta",
                               current_version="0.1.0-beta.3", highest_version="0.1.0-beta.3",
                               minimum_key_epoch=1, data_schema=1)
            events = []
            metadata = artifacts.verify_bundle(
                bundle, signature, keys, work / "verified", **constraints,
                activity=lambda *event: events.append(event))
            self.assertEqual(metadata, built_metadata)
            operations = [event[0] for event in events]
            for operation in ("authenticate", "check_package", "unpack", "save_archive",
                              "archive_layout", "extract", "file_attributes", "check_prepared"):
                self.assertIn(operation, operations)
            self.assertLess(operations.index("authenticate"), operations.index("unpack"))
            for operation, size in (("check_package", catalog["compressed_size"]),
                                    ("unpack", catalog["uncompressed_size"]),
                                    ("extract", metadata["payload_bytes"]),
                                    ("check_prepared", metadata["payload_bytes"])):
                samples = [event[1:] for event in events if event[0] == operation]
                self.assertEqual(samples[0], (0, size))
                self.assertEqual(samples[-1], (size, size))
                self.assertEqual(samples, sorted(samples))
            artifacts.verify_tree(work / "verified", metadata)
            self.assertEqual((work / "verified/root/case").read_bytes(), b"lower")
            self.assertEqual((work / "verified/root/bin/example").stat().st_mode & 0o777, 0o751)
            self.assertEqual(os.getxattr(work / "verified/root/bin/example", "user.cloudplay-test"),
                             b"metadata")
            self.assertEqual(os.readlink(work / "verified/root/absolute-link"), "/bin/example")
            self.assertNotIn("boot/cmdline.txt", metadata["manifest"])
            before = bundle.read_bytes()
            with self.assertRaisesRegex(artifacts.ArtifactError, "IMMUTABLE"):
                builder.build(args)
            self.assertEqual(bundle.read_bytes(), before)
            bundle.write_bytes(before + b"tampered")
            with self.assertRaisesRegex(artifacts.ArtifactError, "SIGNATURE"):
                artifacts.verify_bundle(bundle, signature, keys, work / "tampered", **constraints)


if __name__ == "__main__":
    unittest.main()
