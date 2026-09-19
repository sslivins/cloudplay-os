import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ota_ci_preflight", ROOT / "image-build/preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class UnsignedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(CLOUDPLAY_OTA_EXPERIMENTAL="1", CLOUDPLAY_OTA_VERSION="0.1.0-beta.3",
                        CLOUDPLAY_OTA_MINIMUM_VERSION="0.1.0-beta.2",
                        CLOUDPLAY_OTA_PLATFORM="cm5", CLOUDPLAY_OTA_KEY_EPOCH="1",
                        CLOUDPLAY_OTA_WORKFLOW="test")

    def test_unsigned_mode_requires_public_prerequisites_but_not_private_key(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            preflight.main(payload_only=True)
            with self.assertRaisesRegex(ValueError, "missing CLOUDPLAY_OTA_SECRET_KEY"):
                preflight.main()

    def test_unsigned_mode_rejects_credentials_and_invalid_version(self):
        for key in ("CLOUDPLAY_OTA_SECRET_KEY", "CLOUDPLAY_OTA_SIGNING_KEY"):
            with self.subTest(key=key), mock.patch.dict(
                    os.environ, self.env | {key: "must-not-reach-pi-gen"}, clear=True):
                with self.assertRaisesRegex(ValueError, "must not receive signing credentials"):
                    preflight.main(payload_only=True)
        with mock.patch.dict(os.environ, self.env | {"CLOUDPLAY_OTA_VERSION": "0.1.0"}, clear=True):
            with self.assertRaisesRegex(ValueError, "newer prerelease"):
                preflight.main(payload_only=True)


class SigningWorkflowTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires Bash")
    def test_user_notes_length_and_empty_input_gate(self):
        workflow = (ROOT / ".github/workflows/release-ota.yml").read_text()
        start = workflow.index('if [[ -z "${RELEASE_NOTES')
        validation = workflow[start:workflow.index("\n          fi", start) + len("\n          fi")]
        for notes, accepted in (("", False), (" \n\t", False), ("x" * 320, True),
                                ("x" * 321, False), ("Clearer progress.\nSettings improvements.", True)):
            with self.subTest(length=len(notes)):
                result = subprocess.run(["bash", "-c", validation],
                                        env=dict(os.environ, RELEASE_NOTES=notes),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)

    def test_release_notes_describe_changes_without_packaging_jargon(self):
        workflow = (ROOT / ".github/workflows/release-ota.yml").read_text()
        self.assertIn("RELEASE_NOTES: ${{ inputs.release_notes }}", workflow)
        self.assertIn('--notes "$RELEASE_NOTES"', workflow)
        self.assertIn("Beta release for testing.", workflow)
        self.assertIn('${#RELEASE_NOTES} -gt 320', workflow)
        self.assertNotIn("Signed experimental A/B image and OTA bundle.", workflow)
        self.assertNotIn("Hardware acceptance and publication are separate gates. Build:", workflow)

    def test_release_is_protected_main_only_and_draft(self):
        workflow = (ROOT / ".github/workflows/release-ota.yml").read_text()
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn("environment: ota-release", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('--verify-tag --draft --prerelease', workflow)
        self.assertIn('== "$GITHUB_SHA"', workflow)
        self.assertIn("secrets.CLOUDPLAY_OTA_SIGNING_KEY", workflow)
        self.assertNotIn("secrets.CLOUDPLAY_OTA_RECOVERY", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("build/pi-gen/deploy/experimental-ota/", workflow)
        self.assertNotIn("path: ${{ runner.temp }}", workflow)
        self.assertLess(workflow.index("bash scripts/build-image.sh"),
                        workflow.index("secrets.CLOUDPLAY_OTA_SIGNING_KEY"))
        self.assertIn("CLOUDPLAY_OTA_DEFER_SIGNING=1", workflow)


@unittest.skipUnless(os.name == "posix" and shutil.which("minisign"),
                     "requires Linux minisign for real signing interoperability")
class SigningRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cloudplay-ci-signing-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.runner = self.base / "runner"
        self.bin = self.base / "bin"
        for path in (self.repo / "scripts", self.repo / "image-build/keys",
                     self.runner, self.bin):
            path.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/ci-sign-ota.sh", self.repo / "scripts/ci-sign-ota.sh")
        self.secret = self.base / "primary.key"
        subprocess.run(
            ["minisign", "-G", "-W", "-s", str(self.secret), "-p",
             str(self.repo / "image-build/keys/epoch-1-primary.pub")],
            check=True, capture_output=True, timeout=10)
        (self.repo / "image-build/preflight.py").write_text(
            "import os,pathlib\n"
            "p=pathlib.Path(os.environ['CLOUDPLAY_OTA_SECRET_KEY'])\n"
            "assert p.stat().st_mode & 0o777 == 0o600\n"
            "assert not p.is_relative_to(pathlib.Path.cwd())\n"
            "assert 'CLOUDPLAY_OTA_SIGNING_KEY' not in os.environ\n")
        # Stand in for privilege elevation, executing the exact env invocation.
        (self.bin / "sudo").write_text('#!/bin/bash\nexec "$@"\n')
        (self.bin / "sudo").chmod(0o755)
        (self.repo / "image-build/assemble.py").write_text(
            "import json,os,pathlib\n"
            "assert 'CLOUDPLAY_OTA_SIGNING_KEY' not in os.environ\n"
            "assert 'GH_TOKEN' not in os.environ\n"
            "assert 'GITHUB_TOKEN' not in os.environ\n"
            "assert pathlib.Path(os.environ['CLOUDPLAY_OTA_SECRET_KEY']).is_file()\n"
            "pathlib.Path('build-reached.json').write_text(json.dumps(dict(os.environ)))\n")
        self.env = dict(os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            GITHUB_ACTIONS="true", GITHUB_REF="refs/heads/main",
            GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", RUNNER_TEMP=str(self.runner),
            GH_TOKEN="test-token-not-a-credential", GITHUB_TOKEN="test-token-not-a-credential",
            CLOUDPLAY_OTA_EXPERIMENTAL="1", CLOUDPLAY_OTA_VERSION="0.1.0-beta.3",
            CLOUDPLAY_OTA_MINIMUM_VERSION="0.1.0-beta.2",
            CLOUDPLAY_OTA_PLATFORM="cm5", CLOUDPLAY_OTA_KEY_EPOCH="1",
            CLOUDPLAY_OTA_WORKFLOW="test",
            CLOUDPLAY_OTA_SIGNING_KEY=self.secret.read_text())

    def run_build(self):
        return subprocess.run(
            ["bash", "scripts/ci-sign-ota.sh"], cwd=self.repo, env=self.env,
            capture_output=True, text=True, timeout=20)

    def assert_clean(self):
        self.assertFalse((self.runner / "cloudplay-ota-signing-123-1").exists())

    def test_real_key_match_builds_with_sanitized_environment_and_cleans_secret(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_clean()
        environment = json.loads((self.repo / "build-reached.json").read_text())
        self.assertEqual(environment["CLOUDPLAY_OTA_VERSION"], "0.1.0-beta.3")
        self.assertNotIn(self.env["CLOUDPLAY_OTA_SIGNING_KEY"], result.stdout + result.stderr)

    def test_wrong_key_fails_before_build_and_cleans_secret(self):
        other = self.base / "other.key"
        subprocess.run(["minisign", "-G", "-W", "-s", str(other), "-p",
                        str(self.base / "other.pub")], check=True, capture_output=True, timeout=10)
        self.env["CLOUDPLAY_OTA_SIGNING_KEY"] = other.read_text()
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "build-reached.json").exists())
        self.assert_clean()

    def test_build_failure_cleans_secret(self):
        (self.repo / "image-build/assemble.py").write_text("raise SystemExit(42)\n")
        self.assertEqual(self.run_build().returncode, 42)
        self.assert_clean()

    def test_non_main_and_missing_secret_fail_before_materialization(self):
        for changes in ({"GITHUB_REF": "refs/heads/feature"},
                        {"CLOUDPLAY_OTA_SIGNING_KEY": ""}):
            with self.subTest(changes=list(changes)):
                original = self.env.copy()
                self.env.update(changes)
                self.assertNotEqual(self.run_build().returncode, 0)
                self.assert_clean()
                self.env = original


if __name__ == "__main__":
    unittest.main()
