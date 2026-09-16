import copy
import hashlib
import importlib.util
import io
import json
import re
import shutil
import tarfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifacts = load("artifacts", "artifacts.py")
installer = load("installer", "install-extension.py")
hdr = load("hdr", "hdr-readiness.py")


class HdrPrerequisiteTest(unittest.TestCase):
    def test_old_labwc_is_blocked(self):
        result = hdr.assess("labwc 0.9.8")
        self.assertEqual(result["assessment"], "blocked-by-labwc-version")

    def test_old_wlroots_is_blocked(self):
        result = hdr.assess("labwc 0.20.0 (+xwayland) wlroots-0.20.0")
        self.assertEqual(result["assessment"], "blocked-by-wlroots-version")

    def test_suitable_versions_are_never_hdr_proof(self):
        result = hdr.assess("labwc 0.20.2 (+xwayland +nls +rsvg +libsfdo) wlroots-0.20.2")
        self.assertEqual(result["assessment"], "version-floors-met-hdr-still-unvalidated")
        self.assertFalse(result["hdr_output_validated"])
        self.assertEqual(result["linked_wlroots_version"], "0.20.2")

    def test_missing_linked_version_is_unknown(self):
        self.assertEqual(hdr.assess("labwc 0.20.2")["assessment"], "unknown-version-prerequisites")

    def test_unrecognized_or_prerelease_is_unknown(self):
        self.assertEqual(hdr.assess("unexpected")["assessment"], "unknown-version-prerequisites")
        self.assertEqual(
            hdr.assess("labwc 0.20.0-rc1 wlroots-0.20.1")["assessment"],
            "unknown-prerelease-version",
        )

    def test_missing_binary_is_reported_without_enabling_anything(self):
        with patch.object(hdr.subprocess, "run", side_effect=FileNotFoundError("no labwc")) as run:
            result = hdr.probe()
        self.assertEqual(result["assessment"], "unknown-version-prerequisites")
        self.assertIn("probe_error", result)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/labwc", "--version"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)


class PinsTest(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT / "manifest.json").read_text())

    def finalized(self):
        value = copy.deepcopy(self.lock)
        value["extension"].update(commit="a" * 40, sha256="b" * 64)
        return value

    def test_checked_in_manifest(self):
        artifacts.validate(self.lock, allow_unset=True)

    def test_unset_is_never_buildable(self):
        value = self.finalized()
        value["extension"].update(commit=artifacts.COMMIT_UNSET, sha256=artifacts.HASH_UNSET)
        with self.assertRaises(ValueError):
            artifacts.validate(value)

    def test_finalized_manifest(self):
        artifacts.validate(self.finalized())

    def test_package_version_is_distinct_from_asset_filename_version(self):
        value = self.finalized()
        self.assertEqual(value["browser"]["package_version"], "1:152.0.7977.75-1~deb13u1+rpt1")
        self.assertNotEqual(value["browser"]["package_version"], value["browser"]["version"])
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn('lock["browser"]["package_version"]', stage)
        value["browser"]["package_version"] = "bad version"
        with self.assertRaises(ValueError):
            artifacts.validate(value)

    def test_floating_or_partial_extension_rejected(self):
        for commit in ("main", "v1.0", "abcdef0", "a" * 39):
            value = self.finalized()
            value["extension"]["commit"] = commit
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                artifacts.validate(value, allow_unset=True)

    def test_digest_and_package_set_enforced(self):
        for field, replacement in (("sha256", "bad"), ("filename", "../../browser.deb")):
            value = self.finalized()
            value["browser"]["assets"][0][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                artifacts.validate(value)
        value = self.finalized()
        value["browser"]["assets"].pop()
        with self.assertRaises(ValueError):
            artifacts.validate(value)

    def test_no_partially_unset_extension(self):
        value = self.finalized()
        value["extension"]["sha256"] = artifacts.HASH_UNSET
        with self.assertRaises(ValueError):
            artifacts.validate(value, allow_unset=True)


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "build" / "tests" / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.work))
        self.target = self.work / "extension"
        self.target.mkdir()
        self.commit = "a" * 40
        self.prefix = f"gfn-pi-compat-{self.commit}"

    def archive(self, extra=None, mv=3):
        path = self.work / "source.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            data = json.dumps({"manifest_version": mv, "name": "fixture", "version": "1"}).encode()
            member = tarfile.TarInfo(f"{self.prefix}/manifest.json")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
            if extra:
                tar.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isfile() else None)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_unpack_mv3(self):
        path, digest = self.archive()
        result = installer.unpack(path, self.target, self.commit, digest)
        self.assertEqual(result["manifest_version"], 3)
        self.assertTrue((self.target / "manifest.json").is_file())

    def test_rejects_corrupt_digest(self):
        path, _ = self.archive()
        with self.assertRaisesRegex(ValueError, "SHA256"):
            installer.unpack(path, self.target, self.commit, "0" * 64)

    def test_rejects_traversal_symlink_hardlink(self):
        for kind in ("traversal", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                target = self.work / kind
                target.mkdir()
                member = tarfile.TarInfo(f"{self.prefix}/../escape" if kind == "traversal" else f"{self.prefix}/link")
                if kind != "traversal":
                    member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    member.linkname = "/etc/shadow"
                path, digest = self.archive(member)
                with self.assertRaises(ValueError):
                    installer.unpack(path, target, self.commit, digest)
        self.assertFalse((self.work / "escape").exists())

    def test_wrong_commit_root(self):
        path, digest = self.archive()
        with self.assertRaises(ValueError):
            installer.unpack(path, self.target, "b" * 40, digest)

    def test_mv2_rejected(self):
        path, digest = self.archive(mv=2)
        with self.assertRaisesRegex(ValueError, "MV3"):
            installer.unpack(path, self.target, self.commit, digest)

    def test_verifier(self):
        path, digest = self.archive()
        artifacts.verify(path, digest)
        with self.assertRaises(ValueError):
            artifacts.verify(path, "0" * 64)


class InputStagingTest(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "build" / "tests" / uuid.uuid4().hex
        self.source = self.work / "cache"
        self.source.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.work))
        self.destination = self.work / "staged"
        self.lock = json.loads((ROOT / "manifest.json").read_text())
        self.payload = b"verified test artifact"
        digest = hashlib.sha256(self.payload).hexdigest()
        for asset in self.lock["browser"]["assets"]:
            asset["sha256"] = digest
            (self.source / asset["filename"]).write_bytes(self.payload)
        self.lock["extension"].update(commit="a" * 40, sha256=digest)
        (self.source / "extension.tar.gz").write_bytes(self.payload)

    def test_only_current_manifest_inputs_are_staged(self):
        (self.source / "chromium_old_arm64.deb").write_bytes(b"old browser")
        artifacts.stage(self.lock, self.source, self.destination)
        expected = {asset["filename"] for asset in self.lock["browser"]["assets"]}
        expected.add("extension.tar.gz")
        self.assertEqual({path.name for path in self.destination.iterdir()}, expected)
        self.assertTrue(all(path.read_bytes() == self.payload for path in self.destination.iterdir()))
        script = (ROOT / "scripts/build-image.sh").read_text()
        self.assertIn("artifacts.py stage --destination", script)
        self.assertNotIn("cp -a build/artifacts", script)

    def test_corrupt_input_fails_before_staging(self):
        (self.source / "extension.tar.gz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            artifacts.stage(self.lock, self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_is_never_merged(self):
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            artifacts.stage(self.lock, self.source, self.destination)


class RecipeSafetyTest(unittest.TestCase):
    def test_onboarding_and_ssh_defaults(self):
        config = (ROOT / "config").read_text()
        for expected in ("FIRST_USER_PASS=''", "DISABLE_FIRST_BOOT_USER_RENAME=0",
                         "ENABLE_SSH=0", "PASSWORDLESS_SUDO=0"):
            self.assertIn(expected, config)
        self.assertIn("stage3 stage-cloudplay", config)

    def test_launcher_security(self):
        launcher = (ROOT / "stage-cloudplay/00-appliance/files/cloudplay-start").read_text()
        for forbidden in ("--no-sandbox", "--disable-setuid-sandbox", "--remote-debugging",
                          "--force-color-profile", "--ignore-certificate-errors", "sudo ",
                          "--window-size", "--force-device-scale-factor", "--disable-hdr"):
            self.assertNotIn(forbidden, launcher)
        for expected in ("chmod 700", "--load-extension=/opt/gfn-pi-compat",
                         "piwiz.desktop", "rpi-first-boot-wizard", "--ozone-platform=wayland"):
            self.assertIn(expected, launcher)

    def test_no_release_or_automatic_image_build(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("ubuntu-24.04-arm", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("push:", workflow)

    def test_workflow_actions_are_commit_pinned(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            references = re.findall(r"uses:\s+([^\s#]+)", path.read_text())
            self.assertTrue(references, path.name)
            for reference in references:
                with self.subTest(workflow=path.name, action=reference):
                    self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
