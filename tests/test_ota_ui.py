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
