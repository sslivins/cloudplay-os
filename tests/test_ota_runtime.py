import copy
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import shutil
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from updater.artifacts import GENERATED_PATHS
from updater.runtime import Runtime
from updater.service import Service
from updater.state import Config, Journal, UpdateError, atomic_write, read_json


class FakeDiscovery:
    last_successful_check = 10
    next_check_at = 20

    def check(self, current, *, force):
        return {"version": "1.1.0+release", "notes": "new", "size": 42}

    def download(self, release, directory, progress, cancel, *, activity=None):
        progress(42, 42)
        return directory / "bundle", directory / "signature"


class FakePlatform:
    def __init__(self):
        self.active = "A"
        self.calls = []
        self.config = None
        self.fail_health = False
        self.fail_verify = False
        self.staging_failure = False
        self.guard = dict(attempted=False, confirmed=False, deadline_seconds=90)

    def inspect(self):
        return type("Layout", (), {"active": self.active,
                                   "target": "B" if self.active == "A" else "A"})()

    def boot_id(self):
        return "boot1"

    def guard_state(self, pending=None):
        return self.guard

    def candidate_guard_attempted(self, layout, pending):
        return self.guard["attempted"]

    @contextmanager
    def guard_lock(self):
        yield

    def mark_guard(self, *, confirmed=False):
        self.guard["confirmed" if confirmed else "attempted"] = True

    def precheck(self):
        self.calls.append("precheck")
        return self.inspect()

    @contextmanager
    def provider_lock(self):
        self.calls.append("provider_lock")
        yield

    @contextmanager
    def inhibitor(self):
        self.calls.append("inhibitor")
        yield lambda: True

    def verify_good(self, layout, identity):
        self.calls.append("verify_good")

    def verify_candidate(self, layout, pending, *, activity=None):
        self.calls.append("verify_candidate")
        if self.fail_verify:
            raise UpdateError("SLOT_VERIFY", "changed target")

    def stage(self, source, metadata, layout, checkpoint, *, progress=None, activity=None):
        self.calls.append("stage")
        checkpoint("invalidating", None)
        if self.staging_failure:
            raise UpdateError("WRITE", "injected write error")
        for phase in ("staging_boot", "staging_root", "verifying_slot",
                      "publishing", "ready_to_restart"):
            checkpoint(phase, {"boot/slot-valid.json": {}})

    def write_pointers(self, layout, default, candidate):
        self.calls.append(("pointer", default, candidate))

    def reboot(self, *, tryboot=False):
        self.calls.append(("reboot", tryboot))

    def quarantine_running(self):
        self.calls.append("quarantine")

    def health(self, pending, *, now=None):
        if self.fail_health:
            raise UpdateError("HEALTH", "injected stale heartbeat")
        return self.inspect()


class HealthTimingConfigTests(unittest.TestCase):
    def test_default_and_bounds_preserve_recovery_deadline(self):
        self.assertEqual(Config().stabilization_seconds, 10)
        self.assertEqual(Config().deadline_seconds, 90)
        Config().validate()
        for seconds in (0, 9, 90):
            with self.subTest(seconds=seconds), self.assertRaises(UpdateError):
                replace(Config(), stabilization_seconds=seconds).validate()

    def test_legacy_default_is_adapted_without_rewriting_shared_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            policy = dict(stabilization_seconds=120, deadline_seconds=600)
            path.write_text(json.dumps(policy))
            before = path.read_bytes()
            with patch("updater.state.trusted_path"), \
                    self.assertLogs("cloudplay.updater", level="INFO"):
                loaded = Config.load(path)
            self.assertEqual(loaded.stabilization_seconds, 10)
            self.assertEqual(loaded.deadline_seconds, 90)
            self.assertEqual(path.read_bytes(), before)
            for seconds in (10, 30, 60):
                path.write_text(json.dumps(dict(policy, stabilization_seconds=seconds)))
                with patch("updater.state.trusted_path"):
                    self.assertEqual(Config.load(path).stabilization_seconds, seconds)
            path.write_text(json.dumps(dict(stabilization_seconds=180, deadline_seconds=900)))
            with patch("updater.state.trusted_path"):
                loaded = Config.load(path)
            self.assertEqual((loaded.stabilization_seconds, loaded.deadline_seconds), (180, 900))

    def test_legacy_deadline_cannot_leave_impossible_health_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(dict(stabilization_seconds=90, deadline_seconds=600)))
            with patch("updater.state.trusted_path"), self.assertRaisesRegex(UpdateError, "health time bounds"):
                Config.load(path)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(state_dir=str(self.root), release_file=str(self.root / "release.json"))
        atomic_write(self.root / "release.json", b'{"version":"1.0.0"}')
        self.platform = FakePlatform()
        self.meta = dict(version="1.1.0+release", key_epoch=2,
                         manifest_sha256="a" * 64,
                         manifest={"boot/config.txt": {"sha256": "b" * 64}})
        self.time = 20.0
        self.verifier = Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(self.meta))
        self.runtime = Runtime(self.config, platform=self.platform, discovery=FakeDiscovery(),
                               verifier=self.verifier, clock=lambda: self.time)
        self.runtime.journal.initialize("1.0.0", "A", 1)
        self.runtime.journal.update(
            phase="available", available=dict(version="1.1.0+release", notes="hello", size=42),
            last_good_identity=dict(slot="A", version="1.0.0", manifest_sha256="c" * 64,
                                    config_sha256="d" * 64))
        self.gate = patch.object(Config, "mutation_gate")
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.contract = patch("updater.runtime.GENERATED_PATHS", GENERATED_PATHS | {"boot/autoboot.txt"})
        self.contract.start()
        self.addCleanup(self.contract.stop)
        self.trusted = patch("updater.runtime.trusted_path")
        self.trusted.start()
        self.addCleanup(self.trusted.stop)

    def workspace(self, *, bound=True):
        path = self.root / ("release-" + "a" * 32)
        path.mkdir(mode=0o700)
        (path / "payload").write_bytes(b"interrupted download")
        record = dict(name=path.name, parent_inode=self.root.stat().st_ino,
                      inode=path.stat().st_ino if bound else None)
        self.runtime.journal.update(staging=record)
        return path, record

    def test_workspace_bound_before_download_and_cleared_after_cleanup(self):
        download = self.runtime.discovery.download
        def inspect_download(release, directory, *args, **kwargs):
            record = self.runtime.journal.load()["staging"]
            self.assertEqual(record["name"], directory.parent.name)
            self.assertEqual(record["inode"], directory.parent.stat().st_ino)
            return download(release, directory, *args, **kwargs)
        self.runtime.discovery.download = inspect_download
        self.runtime.install()
        self.assertIsNone(self.runtime.journal.load()["staging"])

    def test_reservation_precedes_mkdir_and_binding_precedes_payload(self):
        mkdir = Path.mkdir
        def inspect_mkdir(path, *args, **kwargs):
            if path.name.startswith("release-"):
                record = self.runtime.journal.load()["staging"]
                self.assertEqual(record["name"], path.name)
                self.assertIsNone(record["inode"])
            elif path.name == "download":
                self.assertEqual(self.runtime.journal.load()["staging"]["inode"],
                                 path.parent.stat().st_ino)
            return mkdir(path, *args, **kwargs)
        with patch.object(Path, "mkdir", new=inspect_mkdir):
            self.runtime.install()

    def test_reboot_preserves_workspace_until_retry_then_reclaims_before_precheck(self):
        for phase in ("downloading", "verifying", "staging_root"):
            with self.subTest(phase=phase):
                self.runtime.journal.update(phase="available", pending=None)
                self.runtime.install()
                path, record = self.workspace()
                self.runtime.journal.update(phase=phase)
                restored = Runtime(self.config, platform=self.platform,
                                   discovery=FakeDiscovery(), verifier=self.verifier)
                with patch.object(restored, "_cleanup_staging",
                                  side_effect=AssertionError("cleanup delayed boot")):
                    self.assertEqual(restored.reconcile()["phase"], "failed")
                state = restored.journal.load()
                self.assertIsNone(state["pending"])
                self.assertEqual(state["staging"], record)
                self.assertEqual(state["highest_version"], self.meta["version"])
                self.assertEqual(state["minimum_key_epoch"], 2)
                self.assertTrue(path.exists())
                restored.check()
                precheck = self.platform.precheck
                def check_after_cleanup():
                    self.assertFalse(path.exists())
                    self.assertIsNone(restored.journal.load()["staging"])
                    self.assertEqual(self.platform.calls[-1], "verify_good")
                    with self.assertRaisesRegex(UpdateError, "BUSY"):
                        with restored.journal.operation():
                            pass
                    return precheck()
                with patch.object(self.platform, "precheck", side_effect=check_after_cleanup):
                    restored.install()
                self.assertIsNone(restored.journal.load()["staging"])

    def test_unknown_workspaces_and_other_state_survive_retry(self):
        for name in ("release-unknown", "release-" + "b" * 32, "lab-assets", "profiles", "mounts"):
            directory = self.root / name
            directory.mkdir()
            (directory / "keep").write_text("untouched")
        self.runtime.install()
        for name in ("release-unknown", "release-" + "b" * 32, "lab-assets", "profiles", "mounts"):
            self.assertEqual((self.root / name / "keep").read_text(), "untouched")

    def test_unused_reservation_can_be_cleared_but_existing_unbound_path_cannot(self):
        path, record = self.workspace(bound=False)
        with self.assertRaisesRegex(UpdateError, "unbound or changed"):
            self.runtime.install()
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.assertTrue((path / "payload").exists())
        self.assertNotIn("precheck", self.platform.calls)
        shutil.rmtree(path)
        self.runtime.install()
        self.assertIsNone(self.runtime.journal.load()["staging"])

    def test_collision_is_never_adopted(self):
        path, _ = self.workspace()
        self.runtime.journal.update(staging=None)
        with patch("updater.runtime.uuid.uuid4", return_value=Mock(hex="a" * 32)), \
                self.assertRaisesRegex(UpdateError, "refusing to adopt"):
            self.runtime.install()
        self.assertTrue((path / "payload").exists())
        self.assertIsNone(self.runtime.journal.load()["staging"])

    def test_binding_failure_leaves_unbound_empty_directory_and_no_download(self):
        update = self.runtime.journal.update
        def fail_binding(**changes):
            if changes.get("staging", {}).get("inode") is not None:
                raise OSError("binding write failed")
            return update(**changes)
        with patch.object(self.runtime.journal, "update", side_effect=fail_binding), \
                patch.object(self.runtime.discovery, "download") as download, \
                self.assertRaisesRegex(OSError, "binding write"):
            self.runtime.install()
        download.assert_not_called()
        record = self.runtime.journal.load()["staging"]
        self.assertIsNone(record["inode"])
        self.assertEqual(list((self.root / record["name"]).iterdir()), [])

    def test_reservation_failure_creates_no_workspace(self):
        with patch.object(self.runtime.journal, "update", side_effect=OSError("reservation failed")), \
                self.assertRaisesRegex(OSError, "reservation failed"):
            self.runtime.install()
        self.assertFalse(list(self.root.glob("release-*")))
        self.assertIsNone(self.runtime.journal.load().get("staging"))

    def test_directory_sync_failure_before_binding_never_downloads(self):
        with patch("updater.runtime.sync_directory", side_effect=OSError("mkdir fsync failed")), \
                patch.object(self.runtime.discovery, "download") as download, \
                self.assertRaisesRegex(OSError, "mkdir fsync failed"):
            self.runtime.install()
        download.assert_not_called()
        record = self.runtime.journal.load()["staging"]
        self.assertIsNone(record["inode"])
        self.assertEqual(list((self.root / record["name"]).iterdir()), [])

    def test_partial_cleanup_keeps_record_and_next_retry_completes(self):
        path, record = self.workspace()
        (path / "remaining").write_text("keep until retry")
        def partial_cleanup(directory):
            (directory / "payload").unlink()
            raise OSError("injected deletion failure")
        with patch("updater.runtime.shutil.rmtree", side_effect=partial_cleanup), \
                self.assertRaisesRegex(OSError, "deletion failure"):
            self.runtime.install()
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.assertTrue((path / "remaining").exists())
        self.assertNotIn("precheck", self.platform.calls)
        self.runtime.install()
        self.assertFalse(path.exists())
        self.assertIsNone(self.runtime.journal.load()["staging"])

    def test_cleanup_sync_failure_retains_record_even_after_directory_removed(self):
        path, record = self.workspace()
        with patch("updater.runtime.sync_directory", side_effect=OSError("fsync failed")), \
                self.assertRaisesRegex(OSError, "fsync failed"):
            self.runtime.install()
        self.assertFalse(path.exists())
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.runtime.install()
        self.assertIsNone(self.runtime.journal.load()["staging"])

    def test_record_clear_failure_retries_absent_workspace_idempotently(self):
        path, record = self.workspace()
        update = self.runtime.journal.update
        def fail_clear(**changes):
            if changes == {"staging": None}:
                self.assertFalse(path.exists())
                raise OSError("journal clear failed")
            return update(**changes)
        with patch.object(self.runtime.journal, "update", side_effect=fail_clear), \
                self.assertRaisesRegex(OSError, "journal clear failed"):
            self.runtime.install()
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.runtime.install()
        self.assertIsNone(self.runtime.journal.load()["staging"])

    def test_workspace_and_parent_identity_mismatch_refused(self):
        path, record = self.workspace()
        for field in ("inode", "parent_inode"):
            self.runtime.journal.update(staging=dict(record, **{field: record[field] + 1}))
            with self.subTest(field=field), self.assertRaisesRegex(UpdateError, "STAGING"):
                self.runtime.install()
            self.assertTrue((path / "payload").exists())

    @unittest.skipUnless(os.name == "posix", "Linux symlink and mount protections")
    def test_workspace_symlink_rejected_but_payload_links_are_not_followed(self):
        path, record = self.workspace()
        external = self.root / "external"
        external.mkdir()
        (external / "keep").write_text("untouched")
        shutil.rmtree(path)
        path.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(UpdateError, "STAGING"):
            self.runtime.install()
        path.unlink()
        path.mkdir(mode=0o700)
        (path / "link").symlink_to(external, target_is_directory=True)
        self.runtime.journal.update(staging=dict(record, inode=path.stat().st_ino))
        self.runtime.install()
        self.assertEqual((external / "keep").read_text(), "untouched")

    @unittest.skipUnless(os.name == "posix", "Linux mount table")
    def test_workspace_or_nested_same_device_mount_refused(self):
        path, record = self.workspace()
        for mount in (path, path / "nested"):
            text = f"20 10 179:6 / {mount} rw - ext4 /dev/mmcblk0p6 rw\n"
            with self.subTest(mount=mount), patch.object(Path, "read_text", return_value=text), \
                    self.assertRaisesRegex(UpdateError, "contains a mount"):
                self.runtime.install()
            self.assertEqual(self.runtime.journal.load()["staging"], record)
            self.assertTrue((path / "payload").exists())

    @unittest.skipUnless(os.name == "posix", "Linux workspace permissions")
    def test_nonprivate_workspace_is_not_removed(self):
        path, record = self.workspace()
        path.chmod(0o755)
        with self.assertRaisesRegex(UpdateError, "owned privately"):
            self.runtime.install()
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.assertTrue((path / "payload").exists())

    def test_recovered_restart_requires_cleanup_before_candidate_readback(self):
        self.runtime.install()
        path, record = self.workspace()
        with patch("updater.runtime.shutil.rmtree", side_effect=OSError("cleanup failed")), \
                self.assertRaisesRegex(OSError, "cleanup failed"):
            self.runtime.restart()
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.assertNotIn("verify_candidate", self.platform.calls)
        self.assertNotIn(("reboot", True), self.platform.calls)
        with patch.object(self.platform, "verify_candidate",
                          side_effect=lambda *args, **kwargs: self.assertFalse(path.exists())):
            self.runtime.restart()
        self.assertIn(("reboot", True), self.platform.calls)

    def test_candidate_health_and_rollback_do_not_wait_for_staging_cleanup(self):
        self.test_candidate_recognized_without_dt_tryboot()
        path, record = self.workspace()
        with patch.object(self.runtime, "_cleanup_staging",
                          side_effect=AssertionError("cleanup delayed candidate health")):
            self.runtime.reconcile(boot_id="boot1")
            self.runtime.health()
            self.time = 700
            self.runtime.health(deadline_only=True)
        self.assertTrue(path.exists())
        self.assertEqual(self.runtime.journal.load()["staging"], record)
        self.assertIn(("reboot", False), self.platform.calls)

    def test_check_failure_preserves_context_and_retry_clears_error(self):
        with patch.object(self.runtime.discovery, "check",
                          side_effect=UpdateError("NETWORK", "Offline")):
            with self.assertRaises(UpdateError):
                self.runtime.check()
        status = self.runtime.status()
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["error"]["command"], "check")
        self.assertEqual(status["error"]["code"], "NETWORK")
        with patch.object(self.runtime.discovery, "check", return_value=None):
            status = self.runtime.check()
        self.assertEqual(status["phase"], "idle")
        self.assertIsNone(status["error"])
        self.assertEqual(status["last_successful_check"], 10)

    def test_beta_preference_persists_and_clears_previous_channel_offer(self):
        before = self.runtime.journal.load()
        status = self.runtime.disable_beta()
        self.assertEqual(status["channel"], "stable")
        self.assertIsNone(status["available_version"])
        self.assertEqual(self.runtime.discovery.channel, "stable")
        state = self.runtime.journal.load()
        for field in ("current_version", "highest_version", "minimum_key_epoch", "last_good"):
            self.assertEqual(state[field], before[field])
        restored = Runtime(self.config, platform=self.platform)
        self.assertEqual(restored.status()["channel"], "stable")
        self.assertEqual(restored.discovery.channel, "stable")
        self.assertEqual(restored.enable_beta()["channel"], "beta")
        self.assertEqual(restored.discovery.channel, "beta")

    def test_channel_change_blocked_during_update_and_recovery(self):
        for phase in ("checking", "downloading", "verifying", "ready_to_restart",
                      "tryboot_running", "promoting", "recovery_required"):
            with self.subTest(phase=phase):
                self.runtime.journal.update(phase=phase)
                with self.assertRaisesRegex(UpdateError, "BUSY"):
                    self.runtime.disable_beta()
                self.assertEqual(self.runtime.status()["channel"], "beta")

    def test_invalid_persisted_channel_is_rejected(self):
        self.runtime.journal.update(channel="nightly")
        with self.assertRaisesRegex(UpdateError, "invalid update channel"):
            self.runtime.status()

    def test_verifier_uses_selected_channel(self):
        self.runtime.disable_beta()
        self.runtime.discovery.check = Mock(return_value=dict(version="1.1.0+release", size=42))
        self.runtime.discovery.download = FakeDiscovery().download
        self.runtime.check()
        self.runtime.install()
        self.assertEqual(self.verifier.call_args.kwargs["channel"], "stable")

    def test_install_ready_preserves_full_metadata_and_floors(self):
        result = self.runtime.install()
        self.assertEqual(result["phase"], "ready_to_restart")
        state = self.runtime.journal.load()
        self.assertEqual(state["pending"]["metadata"], self.meta)
        self.assertEqual(state["highest_version"], "1.1.0+release")
        self.assertEqual(state["minimum_key_epoch"], 2)
        self.assertEqual(state["current_version"], "1.0.0")
        policy = self.verifier.call_args.kwargs
        self.assertEqual(policy["current_version"], "1.0.0")
        self.assertEqual(policy["highest_version"], "1.0.0")
        self.assertFalse(list(self.root.glob("release-*")))

    def test_service_install_cleans_up_then_verifies_and_restarts_automatically(self):
        service = Service(self.runtime)
        cleanup = shutil.rmtree
        order = []
        def remove_staging(path):
            order.append("cleanup")
            self.assertEqual(service.status()["phase"], "finishing")
            self.assertNotIn(("reboot", True), self.platform.calls)
            cleanup(path)
        def readback(layout, pending, *, activity=None):
            order.append("readback")
            self.assertEqual(service.status()["phase"], "restarting")
            self.assertFalse(service.status()["can_restart"])
            self.assertFalse(self.runtime._installing)
            self.assertFalse(list(self.root.glob("release-*")))
            self.assertFalse(self.runtime.journal.load()["pending"]["attempted"])
        self.platform.verify_candidate = readback
        service._command = "install"
        service._worker_lock.acquire()
        with patch("updater.runtime.shutil.rmtree", side_effect=remove_staging):
            service._work("install")
        self.assertEqual(order, ["cleanup", "readback"])
        self.assertEqual(self.platform.calls[-1], ("reboot", True))
        self.assertEqual(self.platform.calls.count(("reboot", True)), 1)
        self.assertTrue(self.runtime.journal.load()["pending"]["attempted"])
        self.assertIsNone(self.runtime.status()["error"])

    def test_service_cleanup_failure_never_activates_or_restarts(self):
        service = Service(self.runtime)
        service._command = "install"
        service._worker_lock.acquire()
        with patch("updater.runtime.shutil.rmtree", side_effect=OSError("cleanup failed")), \
                self.assertLogs("cloudplay.updater", level="ERROR"):
            service._work("install")
        self.assertEqual(self.runtime.status()["phase"], "failed")
        self.assertFalse(self.runtime.journal.load()["pending"]["attempted"])
        self.assertNotIn(("reboot", True), self.platform.calls)
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "pointer"
                             for call in self.platform.calls))

    def test_cleanup_identity_error_is_failed_not_restart_ready(self):
        with patch.object(self.runtime, "_cleanup_staging", side_effect=[
                None, UpdateError("STAGING", "workspace changed")]), \
                self.assertRaisesRegex(UpdateError, "workspace changed"):
            self.runtime.install()
        status = self.runtime.status()
        self.assertEqual(status["phase"], "failed")
        self.assertFalse(status["can_restart"])
        self.assertEqual(status["error"]["code"], "STAGING")
        self.assertIsNotNone(self.runtime.journal.load()["staging"])
        self.assertNotIn(("reboot", True), self.platform.calls)

    def test_service_failed_readback_leaves_explicit_finish_action(self):
        self.platform.fail_verify = True
        service = Service(self.runtime)
        service._command = "install"
        service._worker_lock.acquire()
        with self.assertLogs("cloudplay.updater", level="ERROR"):
            service._work("install")
        status = service.status()
        self.assertEqual(status["phase"], "ready_to_restart")
        self.assertTrue(status["can_restart"])
        self.assertEqual(status["error"]["command"], "restart")
        self.assertFalse(self.runtime.journal.load()["pending"]["attempted"])
        self.assertNotIn(("reboot", True), self.platform.calls)
        with patch.object(self.runtime, "restart") as retry, \
                patch("updater.service.atomic_write"):
            service._tick()
            service._tick()
        retry.assert_not_called()

    def test_exact_release_identity_not_semver_equality(self):
        self.meta["version"] = "1.1.0+other"
        with self.assertRaisesRegex(UpdateError, "RELEASE_IDENTITY"):
            self.runtime.install()
        self.assertNotIn("stage", self.platform.calls)
        self.assertEqual(self.runtime.journal.load()["highest_version"], "1.0.0")

    def test_progress_is_volatile_phase_scoped_and_not_a_shared_mutable_response(self):
        self.runtime.journal.update(phase="staging_root", progress=None)
        with patch.object(self.runtime.journal, "save") as save:
            self.runtime._report_progress("staging_root", 17, 100)
            self.assertEqual(self.runtime.status()["progress"], dict(received=17, total=100))
            self.runtime.status()["progress"]["received"] = 90
            self.assertEqual(self.runtime.status()["progress"]["received"], 17)
            save.assert_not_called()
        self.runtime.journal.update(phase="verifying_slot")
        self.assertIsNone(self.runtime.status()["progress"])
        self.assertIsNone(self.runtime.journal.load()["progress"])

    def test_install_wires_measured_copy_progress_and_clears_at_next_phase(self):
        def stage(source, metadata, layout, checkpoint, *, progress, activity):
            checkpoint("staging_root", None)
            progress("staging_root", 25, 100)
            self.assertEqual(self.runtime.status()["progress"], dict(received=25, total=100))
            self.assertIsNone(self.runtime.journal.load()["progress"])
            checkpoint("verifying_slot", {})
            self.assertIsNone(self.runtime.status()["progress"])
            checkpoint("ready_to_restart", {})
        self.platform.stage = stage
        self.assertIsNone(self.runtime.install()["progress"])

    def test_cancel_during_verification_prevents_first_destructive_write(self):
        def verifier(*args, **kwargs):
            self.runtime.cancel()
            return self.meta
        self.runtime.verifier = verifier
        with self.assertRaisesRegex(UpdateError, "CANCELLED"):
            self.runtime.install()
        self.assertNotIn("stage", self.platform.calls)
        self.assertIsNone(self.runtime.journal.load()["pending"])

    def test_cancel_after_invalidation_refused(self):
        self.runtime.journal.update(phase="invalidating")
        with self.assertRaisesRegex(UpdateError, "CANCEL_TOO_LATE"):
            self.runtime.cancel()

    def test_failure_after_invalidation_retains_pending_and_floor(self):
        self.platform.staging_failure = True
        with self.assertRaisesRegex(UpdateError, "WRITE"):
            self.runtime.install()
        state = self.runtime.journal.load()
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(state["pending"]["slot"], "B")
        self.assertEqual(state["highest_version"], self.meta["version"])
        self.runtime.reconcile()
        self.assertIsNone(self.runtime.journal.load()["pending"])
        self.assertEqual(self.runtime.journal.load()["highest_version"], self.meta["version"])

    def test_restart_verifies_target_before_pointer_and_records_attempt(self):
        self.runtime.install()
        result = self.runtime.restart()
        self.assertEqual(result["phase"], "restarting")
        self.assertFalse(result["can_restart"])
        self.assertTrue(self.runtime.journal.load()["pending"]["attempted"])
        self.assertLess(self.platform.calls.index("verify_candidate"),
                        self.platform.calls.index(("pointer", "A", "B")))
        self.assertEqual(self.platform.calls[-1], ("reboot", True))

    def test_changed_target_blocks_restart(self):
        self.runtime.install()
        self.platform.fail_verify = True
        with self.assertRaisesRegex(UpdateError, "SLOT_VERIFY"):
            self.runtime.restart()
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "reboot" for c in self.platform.calls))
        self.assertEqual(self.runtime.status()["phase"], "ready_to_restart")
        self.assertTrue(self.runtime.status()["can_restart"])

    def test_restart_readback_is_visible_without_changing_durable_recovery_phase(self):
        self.runtime.install()
        def verify(layout, pending, *, activity):
            self.assertEqual(self.runtime.status()["phase"], "restarting")
            self.assertFalse(self.runtime.status()["can_restart"])
            self.assertEqual(self.runtime.journal.load()["phase"], "ready_to_restart")
        self.platform.verify_candidate = verify
        self.runtime.restart()

    def test_cleanup_is_not_restart_ready_and_preserves_durable_recovery(self):
        cleanup = shutil.rmtree
        def inspect_cleanup(path):
            status = self.runtime.status()
            self.assertEqual(status["phase"], "finishing")
            self.assertEqual(status["operation"]["name"], "cleanup")
            self.assertFalse(status["can_restart"])
            self.assertEqual(self.runtime.journal.load()["phase"], "ready_to_restart")
            cleanup(path)
        with patch("updater.runtime.shutil.rmtree", side_effect=inspect_cleanup):
            self.runtime.install()
        self.assertTrue(self.runtime.status()["can_restart"])
        self.assertIsNone(self.runtime.status()["operation"])

    def test_activity_elapsed_does_not_invent_progress_and_resets_by_operation(self):
        self.runtime.journal.update(phase="verifying")
        with patch.object(self.runtime.journal, "save") as save:
            self.runtime._verify_activity("unpack", 25, 100)
            self.time += 20
            value = self.runtime.status()["operation"]
            self.assertEqual(value["received"], 25)
            self.assertEqual(value["elapsed"], 20)
            self.assertEqual(value["quiet_seconds"], 20)
            value["received"] = 80
            self.assertEqual(self.runtime.status()["operation"]["received"], 25)
            self.runtime._verify_activity("save_archive")
            self.assertEqual(self.runtime.status()["operation"]["elapsed"], 0)
            self.assertIsNone(self.runtime.status()["operation"]["received"])
            save.assert_not_called()
        self.runtime.journal.update(phase="staging_root")
        self.assertIsNone(self.runtime.status()["operation"])

    def test_verifier_activity_can_stop_cancelled_work_before_staging(self):
        def verify(*args, activity, **kwargs):
            activity("unpack", 10, 100)
            status = self.runtime.cancel()
            self.assertTrue(status["cancellation_requested"])
            self.assertFalse(status["can_cancel"])
            activity("unpack", 20, 100)
            self.fail("Cancellation did not stop verification")
        self.runtime.verifier = verify
        with self.assertRaisesRegex(UpdateError, "CANCELLED"):
            self.runtime.install()
        self.assertNotIn("stage", self.platform.calls)

    def test_restart_not_general_reboot(self):
        with self.assertRaisesRegex(UpdateError, "STATE"):
            self.runtime.restart()
        self.assertNotIn(("reboot", False), self.platform.calls)

    def test_power_loss_ready_reoffers_restart(self):
        self.runtime.install()
        self.assertEqual(self.runtime.reconcile()["phase"], "ready_to_restart")
        self.assertFalse(self.runtime.journal.load()["pending"]["attempted"])

    def test_candidate_recognized_without_dt_tryboot(self):
        self.runtime.install()
        self.platform.active = "B"
        self.assertEqual(self.runtime.reconcile(boot_id="boot1")["phase"], "tryboot_running")
        pending = self.runtime.journal.load()["pending"]
        self.assertEqual(pending["boot_id"], "boot1")
        self.assertEqual(pending["deadline"], 90)

    def test_reconcile_same_boot_does_not_extend_deadline(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.time = 40
        self.runtime.reconcile(boot_id="boot1")
        self.assertEqual(self.runtime.journal.load()["pending"]["deadline"], 90)

    def test_legacy_ticket_reconciles_to_90_without_rewriting_ticket(self):
        self.platform.guard["deadline_seconds"] = 600
        self.test_candidate_recognized_without_dt_tryboot()
        self.assertEqual(self.platform.guard["deadline_seconds"], 600)

    def test_legacy_candidate_already_past_90_rolls_back_on_reconcile(self):
        self.runtime.install()
        self.platform.active = "B"
        self.platform.guard["deadline_seconds"] = 600
        self.time = 90
        self.runtime.reconcile(boot_id="boot1")
        self.assertIn(("reboot", False), self.platform.calls)
        self.assertNotIn(("pointer", "B", "A"), self.platform.calls)

    def test_legacy_pending_deadline_does_not_extend_health_wait(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.platform.guard["deadline_seconds"] = 600
        pending = self.runtime.journal.load()["pending"]
        pending["deadline"] = 600
        self.runtime.journal.update(pending=pending)
        self.time = 89.9
        self.runtime.health(deadline_only=True)
        self.assertNotIn(("reboot", False), self.platform.calls)
        self.time = 90
        self.runtime.health(deadline_only=True)
        self.assertIn(("reboot", False), self.platform.calls)

    def test_health_cannot_confirm_at_exact_90_second_boundary(self):
        self.test_candidate_recognized_without_dt_tryboot()
        for self.time in (80, 82, 84, 86, 88):
            self.runtime.health()
        self.time = 90
        self.runtime.health()
        self.assertFalse(self.platform.guard["confirmed"])
        self.assertIn(("reboot", False), self.platform.calls)

    def test_health_requires_continuous_stabilization_then_promotes(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.assertEqual(self.runtime.health()["phase"], "tryboot_running")
        for self.time in (22, 24, 26):
            self.runtime.health()
        self.time = 29.9
        self.assertEqual(self.runtime.health()["phase"], "tryboot_running")
        self.time = 30
        self.assertEqual(self.runtime.health()["phase"], "promoted")
        state = self.runtime.journal.load()
        self.assertEqual(state["last_good"], "B")
        self.assertEqual(state["current_version"], self.meta["version"])
        self.assertIsNone(state["pending"])
        self.assertIn(("pointer", "B", "A"), self.platform.calls)

    def test_health_failure_resets_stabilization(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.health()
        self.time = 29
        self.platform.fail_health = True
        self.runtime.health()
        self.assertIsNone(self.runtime.journal.load()["pending"]["healthy_since"])
        self.platform.fail_health = False
        self.time = 30
        self.assertEqual(self.runtime.health()["phase"], "tryboot_running")
        self.time = 39.9
        self.assertEqual(self.runtime.health()["phase"], "tryboot_running")
        self.time = 40
        self.assertEqual(self.runtime.health()["phase"], "promoted")

    def test_starting_service_retains_diagnostics_and_resets_health_window(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.health()
        deadline = self.runtime.journal.load()["pending"]["deadline"]
        self.time = 29
        with patch.object(self.platform, "health",
                          side_effect=UpdateError("HEALTH_NOT_READY", "startup service is activating")):
            status = self.runtime.health()
        self.assertEqual(status["phase"], "tryboot_running")
        self.assertEqual(status["error"]["code"], "HEALTH_NOT_READY")
        pending = self.runtime.journal.load()["pending"]
        self.assertIsNone(pending["healthy_since"])
        self.assertIsNone(pending["last_health_check"])
        self.assertEqual(pending["deadline"], deadline)
        self.time = 30
        self.assertIsNone(self.runtime.health()["error"])
        self.assertEqual(self.runtime.journal.load()["pending"]["healthy_since"], 30)

    def test_health_waits_for_deadline_probe_lock(self):
        self.test_candidate_recognized_without_dt_tryboot()
        held, release = threading.Event(), threading.Event()

        def deadline_probe():
            with Journal(self.root).operation():
                held.set()
                release.wait(2)

        worker = threading.Thread(target=deadline_probe)
        worker.start()
        timer = threading.Timer(0.15, release.set)
        try:
            self.assertTrue(held.wait(1))
            timer.start()
            self.assertEqual(self.runtime.health()["phase"], "tryboot_running")
            self.assertEqual(self.runtime.journal.load()["pending"]["last_health_check"], self.time)
        finally:
            release.set()
            timer.cancel()
            worker.join(3)

    def test_idle_health_timers_do_not_contend_with_staged_restart(self):
        self.runtime.install()
        with Journal(self.root).operation():
            for deadline_only in (False, True):
                with self.subTest(deadline_only=deadline_only):
                    self.assertEqual(
                        self.runtime.health(deadline_only=deadline_only)["phase"],
                        "ready_to_restart")

    def test_confirmed_boot_marker_recovers_interrupted_promotion_commit(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.health()
        for self.time in (22, 24, 26):
            self.runtime.health()
        self.time = 30
        original = self.runtime.journal.update
        def interrupted(**changes):
            if changes.get("phase") == "promoted":
                raise OSError("injected journal commit failure")
            return original(**changes)
        with patch.object(self.runtime.journal, "update", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.runtime.health()
        self.assertTrue(self.platform.guard["confirmed"])
        self.assertEqual(self.runtime.journal.load()["phase"], "promoting")
        self.time = 900
        self.assertEqual(self.runtime.reconcile(boot_id="newboot")["phase"], "promoted")

    def test_missed_health_samples_restart_stabilization(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.health()
        self.time = 31
        self.assertEqual(self.runtime.health()["phase"], "tryboot_running")
        self.assertEqual(self.runtime.journal.load()["pending"]["healthy_since"], 31)

    def test_early_rollback_attempt_prevents_second_data_driven_reboot(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.platform.guard["attempted"] = True
        with self.assertRaisesRegex(UpdateError, "ROLLBACK_LOOP"):
            self.runtime.health(deadline_only=True)
        self.assertNotIn(("reboot", False), self.platform.calls)

    def test_early_fallback_before_data_reconciliation_records_rollback(self):
        self.runtime.install()
        self.platform.guard["attempted"] = True
        status = self.runtime.reconcile()
        self.assertEqual(status["phase"], "rolled_back")
        self.assertEqual(status["strikes"], 1)
        self.assertIsNone(self.runtime.journal.load()["pending"])

    def test_slow_health_cannot_promote_after_deadline(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.health()
        self.time = 30
        def delayed_health(pending):
            self.time = 90
            return self.platform.inspect()
        self.platform.health = delayed_health
        status = self.runtime.health()
        self.assertEqual(status["phase"], "recovery_required")
        self.assertIn(("reboot", False), self.platform.calls)
        self.assertNotIn(("pointer", "B", "A"), self.platform.calls)

    def check_slow_promotion(self, delay):
        self.test_candidate_recognized_without_dt_tryboot()
        for self.time in (78, 80, 82, 84, 86):
            self.runtime.health()
        self.time = 88

        @contextmanager
        def delayed_lock():
            self.time = 90
            yield

        write_pointers = self.platform.write_pointers

        def delayed_pointers(layout, default, candidate):
            write_pointers(layout, default, candidate)
            self.time = 90

        target = "guard_lock" if delay == "lock" else "write_pointers"
        with patch.object(self.platform, target,
                          delayed_lock if delay == "lock" else delayed_pointers):
            self.runtime.health()
        self.assertFalse(self.platform.guard["confirmed"])
        self.assertIn(("reboot", False), self.platform.calls)
        self.assertEqual(self.platform.calls[-2:], [("pointer", "A", "A"), ("reboot", False)])

    def test_slow_promotion_lock_cannot_confirm_after_deadline(self):
        self.check_slow_promotion("lock")

    def test_slow_pointer_write_cannot_confirm_after_deadline(self):
        self.check_slow_promotion("pointers")

    def test_deadline_quarantines_before_reboot_and_bounds_loop(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.time = 700
        self.runtime.health(deadline_only=True)
        self.assertLess(self.platform.calls.index("quarantine"),
                        self.platform.calls.index(("reboot", False)))
        with self.assertRaisesRegex(UpdateError, "ROLLBACK_LOOP"):
            self.runtime.reconcile(boot_id="boot2")
        self.assertEqual(self.platform.calls.count(("reboot", False)), 1)

    def test_deadline_before_reconcile_cannot_quarantine_last_good(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.platform.active = "A"
        self.time = 700
        with self.assertRaisesRegex(UpdateError, "refusing to quarantine last-good"):
            self.runtime.health(deadline_only=True)
        self.assertNotIn("quarantine", self.platform.calls)
        self.assertNotIn(("reboot", False), self.platform.calls)

    def test_failed_candidate_returns_to_old_profiles_and_records_strike(self):
        self.runtime.install()
        self.runtime.restart()
        result = self.runtime.reconcile(boot_id="oldboot")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(result["strikes"], 1)
        self.assertEqual(result["current_version"], "1.0.0")
        self.assertEqual(result["highest_version"], self.meta["version"])

    def test_unexpected_slot_never_adopted(self):
        self.platform.active = "B"
        with self.assertRaisesRegex(UpdateError, "RECOVERY"):
            self.runtime.reconcile()
        self.assertEqual(self.runtime.journal.load()["last_good"], "A")

    def test_status_never_exposes_urls_or_manifest(self):
        self.runtime.install()
        status = json.dumps(self.runtime.status())
        self.assertNotIn("manifest", status)
        self.assertNotIn("bundle_url", status)

    def test_missing_mirror_contract_is_explicit_blocker(self):
        with patch("updater.runtime.GENERATED_PATHS", frozenset()):
            with self.assertRaisesRegex(UpdateError, "ARTIFACT_CONTRACT"):
                self.runtime.install()
        self.assertNotIn("stage", self.platform.calls)

    def test_graceful_shutdown_not_charged_as_strike(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.shutdown()
        self.platform.active = "A"
        status = self.runtime.reconcile(boot_id="boot2")
        self.assertEqual(status["phase"], "rolled_back")
        self.assertEqual(status["strikes"], 0)

    def test_boot_guard_rollback_is_a_strike_despite_shutdown_hook(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.platform.guard["attempted"] = True
        self.runtime.shutdown()
        self.platform.active = "A"
        status = self.runtime.reconcile(boot_id="boot2")
        self.assertEqual(status["phase"], "rolled_back")
        self.assertEqual(status["strikes"], 1)
        self.assertEqual(status["highest_version"], self.meta["version"])

    def test_recorded_rollback_is_a_strike_despite_graceful_marker(self):
        self.test_candidate_recognized_without_dt_tryboot()
        self.runtime.shutdown()
        state = self.runtime.journal.load()
        state["pending"]["rollback_attempted"] = True
        self.runtime.journal.save(state)
        self.platform.active = "A"
        status = self.runtime.reconcile(boot_id="boot2")
        self.assertEqual(status["strikes"], 1)

    def test_operator_retry_cannot_bypass_pending_candidate(self):
        self.runtime.install()
        with self.assertRaisesRegex(UpdateError, "RECOVERY"):
            self.runtime.retry()

    def test_operator_rollback_not_generic_reboot(self):
        with self.assertRaisesRegex(UpdateError, "RECOVERY"):
            self.runtime.rollback()
        self.assertNotIn(("reboot", False), self.platform.calls)

    def test_strike_limit_blocks_install(self):
        self.runtime.journal.update(strikes=3)
        with self.assertRaisesRegex(UpdateError, "STRIKE_LIMIT"):
            self.runtime.install()


class StateTests(unittest.TestCase):
    def test_optional_staging_record_is_validated_and_preserved_by_updates(self):
        with tempfile.TemporaryDirectory() as name:
            journal = Journal(Path(name))
            journal.initialize("1.0.0", "A", 1)
            original = journal.load()
            self.assertNotIn("staging", original)
            record = dict(name="release-" + "a" * 32, parent_inode=123, inode=456)
            for inode in (None, 456):
                valid = dict(record, inode=inode)
                journal.save(dict(original, staging=valid))
                self.assertEqual(journal.update(notice="legacy update")["staging"], valid)
            invalid = [[], {}, dict(record, name="../profiles"), dict(record, inode=True),
                       dict(record, inode=0), dict(record, parent_inode=False),
                       dict(record, parent_inode=-1), dict(record, extra="unknown")]
            for value in invalid:
                journal.save(dict(original, staging=value))
                with self.subTest(value=value), self.assertRaisesRegex(UpdateError, "staging ownership"):
                    journal.load()
            journal.save(dict(original, staging=None))
            self.assertIsNone(journal.load()["staging"])

    def test_corrupt_phase_or_boolean_schema_is_a_typed_error(self):
        with tempfile.TemporaryDirectory() as name:
            journal = Journal(Path(name))
            journal.initialize("1.0.0", "A", 1)
            original = journal.load()
            for changes in ({"schema": True}, {"phase": {}}, {"phase": "unknown"}):
                journal.save(dict(original, **changes))
                with self.subTest(changes=changes):
                    with self.assertRaisesRegex(UpdateError, "STATE"):
                        journal.load()

    def test_default_configuration_fails_closed(self):
        with self.assertRaisesRegex(UpdateError, "HARDWARE_GATE"):
            Config().mutation_gate()

    def test_same_uid_is_not_isolation(self):
        config = replace(Config(), experimental_hardware_validation=True,
                         hardware_evidence="test", launcher_isolation_verified=True,
                         isolation_evidence="test", launcher_uid=1000, browser_uid=1000)
        with self.assertRaisesRegex(UpdateError, "ISOLATION_GATE"):
            config.mutation_gate()

    def test_no_silent_corrupt_state_reset(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "state.json").write_text("{bad")
            with self.assertRaisesRegex(UpdateError, "STATE"):
                Journal(root).load()
            self.assertEqual((root / "state.json").read_text(), "{bad")

    def test_duplicate_keys_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            path.write_text('{"schema":1,"schema":2}')
            with self.assertRaisesRegex(UpdateError, "STATE"):
                read_json(path)

    def test_operation_lock_excludes_other_journal(self):
        with tempfile.TemporaryDirectory() as name:
            first, second = Journal(Path(name)), Journal(Path(name))
            with first.operation():
                with self.assertRaisesRegex(UpdateError, "BUSY"):
                    with second.operation():
                        self.fail("lock admitted competing writer")

    def test_operation_lock_waits_for_brief_contention(self):
        with tempfile.TemporaryDirectory() as name:
            first, second = Journal(Path(name)), Journal(Path(name))
            started, acquired = threading.Event(), threading.Event()
            errors = []

            def contender():
                started.set()
                try:
                    with second.operation(timeout=2):
                        acquired.set()
                except Exception as exc:
                    errors.append(exc)

            with first.operation():
                worker = threading.Thread(target=contender)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(acquired.wait(0.1))
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(acquired.is_set())

    def test_operation_lock_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as name:
            first, second = Journal(Path(name)), Journal(Path(name))
            with first.operation():
                start = time.monotonic()
                with self.assertRaisesRegex(UpdateError, "BUSY"):
                    with second.operation(timeout=0.1):
                        self.fail("lock admitted competing writer")
                self.assertGreaterEqual(time.monotonic() - start, 0.1)
                self.assertLess(time.monotonic() - start, 2)
            with second.operation():
                pass

    def test_atomic_failure_retains_previous_state(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "value"
            atomic_write(path, b"old")
            with patch("updater.state.os.replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
