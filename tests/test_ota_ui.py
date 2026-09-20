import importlib.util
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location(
    "ota_ui", Path(__file__).resolve().parents[1] / "launcher/updates.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class UpdatePresentationTests(unittest.TestCase):
    def test_check_feedback_is_immediate_even_when_queued_behind_status(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                release = threading.Event()
                def send(command):
                    if not release.wait(3):
                        raise TimeoutError("test request was not released")
                    return dict(phase="checking")
                client = ui.Updates(send)
                self.addCleanup(client.close)
                client.status = dict(phase="available", install_enabled=True,
                                     current_version="1.0.0", available_version="1.1.0",
                                     provider_launch_allowed=True)
                client.error = "Previous connection failed"
                if queued:
                    client.submit("status")
                try:
                    self.assertTrue(client.submit("check"))
                    self.assertEqual(client.error, "")
                    self.assertEqual(client.display_status["phase"], "checking")
                    self.assertEqual(ui.summary(client.display_status), "Checking for updates...")
                    self.assertEqual(ui.version_text(client.display_status), "Cloudplay OS 1.0.0")
                    self.assertEqual(ui.actions(client.display_status), [])
                    self.assertFalse(client.submit("check"))
                finally:
                    release.set()

    def test_check_feedback_clears_for_success_and_failure(self):
        client = ui.Updates(lambda command: {})
        self.addCleanup(client.close)
        client.next_poll = float("inf")
        client.status = dict(phase="idle", current_version="1.0.0")
        for value, error in ((dict(phase="available", install_enabled=True), ""),
                             (None, "Unable to check for updates.")):
            with self.subTest(error=error):
                client.pending, client.active_command = True, "check"
                client.results.put((value, error))
                self.assertTrue(client.poll())
                self.assertNotEqual(client.display_status["phase"], "checking")
                self.assertEqual(client.error, error)
                self.assertIn(("Check for Updates", "check"), ui.actions(client.display_status))

    def test_install_hides_offer_until_acknowledged_including_queued_poll(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                release = threading.Event()
                def send(command):
                    if not release.wait(3):
                        raise TimeoutError("test request was not released")
                    return dict(phase="starting")
                client = ui.Updates(send)
                self.addCleanup(client.close)
                client.status = dict(phase="available", install_enabled=True,
                                     provider_launch_allowed=True)
                if queued:
                    client.submit("status")
                try:
                    self.assertTrue(client.submit("install"))
                    self.assertEqual(client.display_status["phase"], "starting")
                    self.assertEqual(ui.actions(client.display_status), [])
                    self.assertFalse(client.display_status["provider_launch_allowed"])
                    self.assertFalse(client.submit("install"))
                    self.assertEqual(client.status["phase"], "available")
                finally:
                    release.set()

    def test_install_failure_restores_offer_and_surfaces_error(self):
        client = ui.Updates(lambda command: {})
        self.addCleanup(client.close)
        client.status = dict(phase="available", install_enabled=True)
        client.pending, client.active_command = True, "install"
        client.results.put((None, "Unable to start your update."))
        self.assertTrue(client.poll())
        self.assertEqual(client.display_status["phase"], "available")
        self.assertEqual(client.error, "Unable to start your update.")
        self.assertIn(("Install Update", "install"), ui.actions(client.display_status))

    def test_postboot_uses_finishing_copy_not_a_verification_task(self):
        for phase in ("tryboot_running", "promoting"):
            self.assertEqual(ui.summary(dict(phase=phase)), "Finishing your update...")
            self.assertEqual(ui.progress_text(dict(phase=phase)), "")

    def test_menu_transition_does_not_flash_completed_update_actions(self):
        client = ui.Updates(lambda command: {})
        self.addCleanup(client.close)
        client.status = dict(phase="promoted", provider_launch_allowed=True)
        client.pending, client.active_command = True, "close"
        self.assertEqual(client.display_status["phase"], "returning")
        self.assertEqual(ui.actions(client.display_status), [])
        self.assertFalse(client.display_status["provider_launch_allowed"])

    def test_errors_explain_next_steps_without_dumping_internal_details(self):
        for code, expected in (("NETWORK", "internet connection"),
                               ("SPACE", "free space"), ("SIGNATURE", "won't be installed"),
                               ("BACKOFF", "wait a little"), ("BUSY", "Finish the current update"),
                               ("HEALTH", "asking for help")):
            with self.subTest(code=code):
                error = dict(code=code, message="/dev/mmcblk0p5 UID 450 manifest_sha256 failed")
                text = ui.summary(dict(phase="failed", error=error))
                self.assertIn(expected, text)
                self.assertIn("Reference: " + code, text)
                self.assertNotIn(error["message"], text)
        self.assertNotIn("untrusted\ntext", ui.error_detail(dict(code="untrusted\ntext")))
        self.assertTrue(ui.summary(dict(phase="failed", error=dict(code="CANCELLED"))).startswith(
            "Update cancelled."))

    def test_phase_and_task_copy_does_not_expose_implementation_terms(self):
        banned = ("ota", "a/b", "slot", "permissions", "package structure",
                  "measured result", "safety gates", "compositor", "manifest")
        samples = [ui.summary(dict(phase=phase)) for phase in (
            *ui.BUSY, "idle", "available", "ready_to_restart", "promoted",
            "rolled_back", "failed", "disabled", "uninitialized", "recovery_required")]
        samples.append(ui.summary(dict(phase="idle", mutation_enabled=False)))
        for name, (phases, _, measurable) in ui.OPERATIONS.items():
            status = dict(phase=phases[0], operation=dict(
                name=name, received=1 if measurable else None, total=2 if measurable else None,
                quiet_seconds=20, elapsed=60))
            samples.append(ui.summary(status))
        for text in samples:
            for term in banned:
                with self.subTest(text=text, term=term):
                    self.assertNotIn(term, text.lower())
        self.assertNotIn("internal_phase_123", ui.summary(dict(phase="internal_phase_123")))

    def test_available_update_is_not_presented_as_already_installing(self):
        status = dict(phase="available", current_version="1.0.0", available_version="1.1.0",
                      notes="Clearer update progress.")
        self.assertEqual(ui.version_text(status), "Cloudplay OS 1.1.0 is available")
        self.assertIn("What's new:\nClearer update progress.", ui.summary(status))
        status["phase"] = "downloading"
        self.assertEqual(ui.version_text(status), "Updating Cloudplay OS to 1.1.0")

    def test_beta_navigation_requires_root_owned_fixed_page(self):
        path = Mock()
        path.lstat.return_value = Mock(st_mode=ui.stat.S_IFREG | 0o644, st_uid=0, st_size=4)
        path.read_text.return_value = "beta"
        self.assertEqual(ui.initial_page(path), "beta")
        path.read_text.return_value = "enable_beta"
        with self.assertRaisesRegex(ValueError, "Invalid"):
            ui.initial_page(path)
        path.lstat.return_value.st_uid = 1000
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            ui.initial_page(path)
        path.lstat.side_effect = FileNotFoundError
        self.assertEqual(ui.initial_page(path), "updates")

    def test_action_feedback_describes_the_action(self):
        self.assertEqual(ui.request_text("check"), "Checking for updates...")
        self.assertEqual(ui.request_text("install"), "Starting your update...")
        self.assertEqual(ui.request_text("dismiss"), "")
        for command in ui.COMMANDS:
            self.assertNotIn("request", ui.request_text(command).lower())

    def test_unchanged_reply_clears_temporary_action_feedback(self):
        client = ui.Updates(lambda command: {})
        self.addCleanup(client.close)
        client.next_poll = float("inf")
        client.status = dict(phase="idle", last_successful_check=10)
        for command in ("check", "status"):
            with self.subTest(command=command):
                client.pending, client.active_command = True, command
                client.results.put((dict(client.status), ""))
                self.assertTrue(client.poll())
                self.assertFalse(client.pending)
                self.assertIsNone(client.active_command)
                self.assertFalse(client.poll())

    def test_identical_error_reply_also_refreshes_the_screen(self):
        client = ui.Updates(lambda command: {})
        self.addCleanup(client.close)
        client.next_poll = float("inf")
        client.error = "Unavailable"
        client.pending = True
        client.results.put((None, "Unavailable"))
        self.assertTrue(client.poll())
        self.assertEqual(client.error, "Unavailable")
        self.assertFalse(client.pending)

    def test_idle_only_claims_up_to_date_after_successful_check(self):
        self.assertEqual(ui.summary({"phase": "idle"}), "Check for updates")
        for last_check in (None, True, -1, "10", float("nan"), float("inf")):
            with self.subTest(last_check=last_check):
                self.assertEqual(ui.summary(dict(phase="idle", last_successful_check=last_check)),
                                 "Check for updates")
        self.assertEqual(ui.summary(dict(phase="idle", last_successful_check=10)),
                         "Cloudplay OS is up to date.")
        self.assertNotIn("up to date", ui.summary(dict(
            phase="idle", last_successful_check=10, error="Status unavailable")))

    def test_check_failure_is_not_an_install_failure_or_current_success(self):
        error = dict(command="check", code="NETWORK", message="Offline")
        for phase in ("idle", "checking", "failed"):
            text = ui.summary(dict(phase=phase, last_successful_check=10, error=error))
            self.assertEqual(text, "Unable to check for updates.\n"
                                  "Check your internet connection and try again.\nReference: NETWORK")
        error["command"] = "install"
        self.assertTrue(ui.summary(dict(phase="failed", error=error)).startswith(
            "The update could not be completed."))
        error["command"] = "check"
        self.assertIn("back on your previous version", ui.summary(
            dict(phase="rolled_back", error=error)))

    def test_check_request_failure_has_friendly_summary_and_diagnostic(self):
        def send(command):
            raise OSError("Connection refused")
        client = ui.Updates(send)
        self.addCleanup(client.close)
        self.assertTrue(client.submit("check"))
        value, error = client.results.get(timeout=2)
        self.assertIsNone(value)
        self.assertTrue(error.startswith("Unable to check for updates."))
        self.assertIn("Reference: UPDATE_ERROR", error)
        self.assertNotIn("Connection refused", error)

    def test_mutations_require_explicit_service_permission(self):
        for phase, command, flag in (("available", "install", "install_enabled"),
                                     ("ready_to_restart", "restart", "can_restart"),
                                     ("downloading", "cancel", "can_cancel"),
                                     ("verifying", "cancel", "can_cancel")):
            for permission in (None, False):
                self.assertNotIn(command, dict(ui.actions(
                    {"phase": phase, flag: permission})).values())
            self.assertIn(command, dict(ui.actions(
                {"phase": phase, flag: True})).values())

    def test_public_session_only_offers_transition_retry_not_privileged_actions(self):
        status = dict(phase="available", requires_trusted_session=True, install_enabled=True)
        self.assertEqual(ui.actions(status), [("Try Again", "open")])
        self.assertEqual(ui.request_text("open"), "Opening System Updates...")
        self.assertNotIn("browser", ui.summary(status))
        self.assertNotIn("controls", ui.summary(status).lower())

    def test_no_cancel_after_partition_writes_begin(self):
        self.assertIn("cancel", dict(ui.actions({"phase": "downloading", "can_cancel": True})).values())
        for phase in ("staging", "installing", "activating", "tryboot_running",
                      "staging_boot", "staging_root", "verifying_slot", "publishing", "promoting"):
            self.assertEqual(ui.actions({"phase": phase, "install_enabled": True}), [])

    def test_no_manual_refresh_action(self):
        for phase in (*ui.BUSY, "ready_to_restart", "available", "idle", "failed"):
            with self.subTest(phase=phase):
                self.assertNotIn("status", dict(ui.actions({"phase": phase})).values())
        self.assertEqual(ui.actions({"phase": "ready_to_restart", "can_restart": True}),
                         [("Finish Update", "restart")])

    def test_notices_and_progress_are_plain_bounded_text(self):
        self.assertIn("back on your previous version", ui.summary({"phase": "rolled_back"}))
        self.assertIn("17%", ui.summary({"phase": "downloading", "progress": {"received": 17, "total": 100}}))
        self.assertLess(len(ui.summary({"phase": "failed", "error": "x" * 10000})), 500)
        self.assertIn("finish update", ui.badge({"phase": "ready_to_restart"}))
        self.assertNotIn("locked", ui.summary({"phase": "idle", "install_enabled": False,
                                              "mutation_enabled": True}))

    def test_late_cancellation_warns_not_to_disconnect_power(self):
        self.assertIn("Do not disconnect from power.",
                      ui.error_detail({"code": "CANCEL_TOO_LATE"}))

    def test_dismiss_is_presentation_only(self):
        calls = []
        client = ui.Updates(lambda command: calls.append(command))
        self.addCleanup(client.close)
        client.status = {"phase": "rolled_back", "error": {"code": "HEALTH", "message": "Failed"}}
        self.assertTrue(client.submit("dismiss"))
        self.assertEqual(calls, [])
        self.assertEqual(ui.badge(client.status), "Updates")
        self.assertNotIn("dismiss", dict(ui.actions(client.status)).values())

    def test_measured_progress_for_download_and_each_copy_phase(self):
        for phase in ("downloading", "staging_boot", "staging_root"):
            with self.subTest(phase=phase):
                status = dict(phase=phase, progress=dict(received=5 * 1024**2, total=20 * 1024**2))
                self.assertEqual(ui.progress_fraction(status), 0.25)
                self.assertEqual(ui.progress_text(status), "25%")
                self.assertIn("25%", ui.summary(status))
                self.assertNotIn("25%", ui.summary(status, include_progress=False))
                status["progress"]["received"] = 20 * 1024**2 - 1
                self.assertIn("99%", ui.progress_text(status))
                status["progress"]["received"] += 1
                self.assertEqual(ui.progress_fraction(status), 1)
                self.assertIn("100%", ui.progress_text(status))

    def test_unknown_or_invalid_progress_never_invents_a_percentage(self):
        invalid = (None, 50, {}, {"received": 1, "total": 0},
                   {"received": True, "total": 2}, {"received": 1.5, "total": 2},
                   {"received": -1, "total": 2}, {"received": 3, "total": 2},
                   {"received": 1, "total": 2**64}, {"received": 1, "total": "2"})
        for progress in invalid:
            with self.subTest(progress=progress):
                status = dict(phase="staging_root", progress=progress)
                self.assertIsNone(ui.progress_fraction(status))
                self.assertNotIn("Working", ui.progress_text(status))
                self.assertNotIn("%", ui.progress_text(status))
                self.assertNotIn("%", ui.summary(status))
        for phase in ("verifying", "verifying_slot", "publishing", "ready_to_restart",
                      "tryboot_running", "restarting", "failed", "promoted"):
            status = dict(phase=phase, progress=dict(received=3, total=4))
            self.assertIsNone(ui.progress_fraction(status))
            self.assertNotIn("%", ui.summary(status))

    def test_worker_never_blocks_caller_or_duplicates_requests(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def send(command):
            calls.append(command)
            started.set()
            if not release.wait(2):
                raise TimeoutError("fixture timeout")
            return {"phase": "idle", "current_version": "0.1.0-beta.3"}
        client = ui.Updates(send)
        self.addCleanup(client.close)
        self.addCleanup(release.set)
        before = time.monotonic()
        client.poll()
        self.assertLess(time.monotonic() - before, 0.1)
        self.assertTrue(started.wait(1))
        self.assertTrue(client.submit("check"))
        self.assertFalse(client.submit("check"))
        release.set()
        deadline = time.monotonic() + 2
        while client.pending and time.monotonic() < deadline:
            client.poll()
            time.sleep(0.001)
        self.assertEqual(calls, ["status", "check"])
        self.assertEqual(client.status["phase"], "idle")
        with self.assertRaises(ValueError):
            client.submit("https://untrusted.invalid/update")

    def test_measurable_preparation_and_readback_operations(self):
        for phase, name in (("verifying", "unpack"), ("verifying", "extract"),
                            ("verifying", "check_package"), ("verifying", "check_prepared"),
                            ("invalidating", "check_source"), ("verifying_slot", "check_installed"),
                            ("publishing", "check_final"), ("restarting", "check_restart")):
            status = dict(phase=phase, operation=dict(name=name, received=25, total=100, elapsed=40))
            self.assertEqual(ui.progress_fraction(status), .25)
            self.assertIn("25%", ui.progress_text(status))
            self.assertNotIn("Working", ui.summary(status))
            self.assertNotIn("Step", ui.summary(status))
            self.assertNotIn("This measures", ui.summary(status))
            self.assertNotIn("MiB", ui.progress_text(status))
            self.assertNotIn("Update complete", ui.summary(status))

    def test_storage_wait_hides_stale_count_and_reports_elapsed_only(self):
        status = dict(phase="staging_boot", progress=dict(received=100, total=100),
                      operation=dict(name="save_boot", elapsed=65))
        self.assertIsNone(ui.progress_fraction(status))
        self.assertEqual(ui.progress_text(status), "Elapsed: 1:05")
        self.assertIn("Saving startup files", ui.summary(status))
        status["operation"]["name"] = "check_restart"
        self.assertIsNone(ui.progress_fraction(status))
        self.assertNotIn("Checking files before restart", ui.summary(status))

    def test_journey_and_copy_do_not_expose_slots_or_early_success(self):
        for phase in ("invalidating", "staging", "installing", "staging_root", "finishing"):
            self.assertNotIn("slot", ui.summary(dict(phase=phase)).lower())
            self.assertNotIn("restart", dict(ui.actions(dict(phase=phase))).values())
        self.assertEqual(ui.journey(dict(phase="verifying")), (
            ("Download", "done"), ("Prepare", "active"), ("Install", "upcoming"),
            ("Check", "upcoming"), ("Restart", "upcoming")))
        self.assertEqual(ui.journey(dict(phase="promoted")), ())
        self.assertIsNone(ui.step_index(dict(phase="promoted")))
        self.assertEqual(ui.journey(dict(phase="available")), ())
        self.assertNotIn("finished playing", ui.summary(dict(phase="ready_to_restart")))
        self.assertEqual(ui.summary(dict(phase="promoted")), "Check for updates")

    def test_completed_install_does_not_claim_current_discovery_freshness(self):
        status = dict(phase="promoted", last_successful_check=time.time(),
                      notice="Update confirmed successfully", current_version="0.1.0-beta.13")
        self.assertEqual(ui.summary(status), "Check for updates")
        self.assertEqual(ui.journey(status), ())
        self.assertEqual(ui.actions(status), [("Check for Updates", "check")])
        status.update(error=dict(command="close", code="SESSION"))
        self.assertIn("Reference: SESSION", ui.summary(status))

    def test_version_header_is_separate_from_task_status(self):
        status = dict(phase="verifying", current_version="0.1.0-beta.6",
                      available_version="0.1.0-beta.7",
                      operation=dict(name="unpack", received=25, total=100))
        self.assertEqual(ui.version_text(status), "Updating Cloudplay OS to 0.1.0-beta.7")
        self.assertNotIn("0.1.0-beta.6", ui.version_text(status))
        self.assertEqual(ui.summary(status, include_progress=False), "Unpacking update files")
        status.update(candidate_version="0.1.0-beta.8")
        self.assertTrue(ui.version_text(status).endswith("0.1.0-beta.8"))
        self.assertEqual(ui.version_text(dict(current_version="0.1.0-beta.7")),
                         "Cloudplay OS 0.1.0-beta.7")
        for phase in ("idle", "checking", "promoted"):
            self.assertEqual(ui.version_text(dict(phase=phase, current_version="0.1.0-beta.13")),
                             "Cloudplay OS 0.1.0-beta.13")
        self.assertEqual(ui.version_text({}), "Cloudplay OS")
        self.assertEqual(ui.version_text(dict(available_version="0.1.0-beta.7")),
                         "Updating Cloudplay OS to 0.1.0-beta.7")

    def test_cancellation_is_update_not_download_and_pending_is_explained(self):
        self.assertIn(("Cancel Update", "cancel"), ui.actions(dict(phase="verifying", can_cancel=True)))
        self.assertIn("Cancelling your update", ui.summary(
            dict(phase="verifying", cancellation_requested=True)))


if __name__ == "__main__":
    unittest.main()
