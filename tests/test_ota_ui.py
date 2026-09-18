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
        for phase, action in (("downloading", "downloaded"),
                              ("staging_boot", "copied"), ("staging_root", "copied")):
            with self.subTest(phase=phase):
                status = dict(phase=phase, progress=dict(received=5 * 1024**2, total=20 * 1024**2))
                self.assertEqual(ui.progress_fraction(status), 0.25)
                self.assertEqual(ui.progress_text(status), f"25% {action} (5.0 / 20.0 MiB)")
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
                self.assertEqual(ui.progress_text(status), "Working...")
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
        self.assertFalse(client.submit("check"))
        release.set()
        deadline = time.monotonic() + 2
        while client.pending and time.monotonic() < deadline:
            client.poll()
            time.sleep(0.001)
        self.assertEqual(calls, ["status"])
        self.assertEqual(client.status["phase"], "idle")
        with self.assertRaises(ValueError):
            client.submit("https://untrusted.invalid/update")


if __name__ == "__main__":
    unittest.main()
