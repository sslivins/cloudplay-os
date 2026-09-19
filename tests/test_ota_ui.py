import importlib.util
from pathlib import Path
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location(
    "ota_ui", Path(__file__).resolve().parents[1] / "launcher/updates.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class UpdatePresentationTests(unittest.TestCase):
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
            self.assertEqual(text, "Unable to check for updates.\nNETWORK: Offline")
        error["command"] = "install"
        self.assertTrue(ui.summary(dict(phase="failed", error=error)).startswith(
            "The update could not be completed."))
        error["command"] = "check"
        self.assertIn("previous system was restored", ui.summary(
            dict(phase="rolled_back", error=error)))

    def test_check_request_failure_has_friendly_summary_and_diagnostic(self):
        def send(command):
            raise OSError("Connection refused")
        client = ui.Updates(send)
        self.addCleanup(client.close)
        self.assertTrue(client.submit("check"))
        value, error = client.results.get(timeout=2)
        self.assertIsNone(value)
        self.assertEqual(error, "Unable to check for updates.\nConnection refused")

    def test_mutations_require_explicit_service_permission(self):
        for phase, command, flag in (("available", "install", "install_enabled"),
                                     ("ready_to_restart", "restart", "can_restart")):
            for permission in (None, False):
                self.assertNotIn(command, dict(ui.actions(
                    {"phase": phase, flag: permission})).values())
            self.assertIn(command, dict(ui.actions(
                {"phase": phase, flag: True})).values())

    def test_no_cancel_after_partition_writes_begin(self):
        self.assertIn("cancel", dict(ui.actions({"phase": "downloading", "can_cancel": True})).values())
        for phase in ("staging", "installing", "activating", "tryboot_running",
                      "staging_boot", "staging_root", "verifying_slot", "publishing", "promoting"):
            self.assertEqual(ui.actions({"phase": phase, "install_enabled": True}), [])

    def test_notices_and_progress_are_plain_bounded_text(self):
        self.assertIn("previous system was restored", ui.summary({"phase": "rolled_back"}))
        self.assertIn("17%", ui.summary({"phase": "downloading", "progress": {"received": 17, "total": 100}}))
        self.assertLess(len(ui.summary({"phase": "failed", "error": "x" * 10000})), 500)
        self.assertIn("restart ready", ui.badge({"phase": "ready_to_restart"}))
        self.assertNotIn("locked", ui.summary({"phase": "idle", "install_enabled": False,
                                              "mutation_enabled": True}))

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
        self.assertEqual(ui.progress_text(status), "Current task: 1:05 elapsed")
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
        self.assertTrue(all(state == "done" for _, state in ui.journey(dict(phase="promoted"))))
        self.assertEqual(ui.journey(dict(phase="available")), ())
        self.assertNotIn("finished playing", ui.summary(dict(phase="ready_to_restart")))
        self.assertIn("Update complete", ui.summary(dict(phase="promoted")))

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
                         "Cloudplay OS")
        self.assertEqual(ui.version_text({}), "Cloudplay OS")
        self.assertEqual(ui.version_text(dict(available_version="0.1.0-beta.7")),
                         "Updating Cloudplay OS to 0.1.0-beta.7")

    def test_cancellation_is_update_not_download_and_pending_is_explained(self):
        self.assertIn(("Cancel Update", "cancel"), ui.actions(dict(phase="verifying", can_cancel=True)))
        self.assertIn("Cancellation requested", ui.summary(
            dict(phase="verifying", cancellation_requested=True)))


if __name__ == "__main__":
    unittest.main()
