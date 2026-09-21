from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from updater.artifacts import canonical_json
from updater.boot import EarlyGuard, make_ticket, validate_ticket
from updater.state import Config, UpdateError, atomic_write, read_json


class EarlyGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot, self.run_dir = self.root / "boot", self.root / "run"
        self.boot.mkdir()
        self.run_dir.mkdir()
        self.time = 90
        self.events = []
        self.active = "B"
        self.good_error = False
        self.gate = patch.object(Config, "mutation_gate")
        self.gate.start()
        self.addCleanup(self.gate.stop)
        trusted = patch("updater.boot.trusted_path")
        trusted.start()
        self.addCleanup(trusted.stop)
        config = replace(Config(), experimental_hardware_validation=True,
                         hardware_evidence="reviewed physical test",
                         launcher_isolation_verified=True, isolation_evidence="reviewed UID split")
        self.record = dict(schema=1, disk_id="fixture-disk", partitions={})
        previous = dict(slot="A", version="1.0.0", manifest_sha256="a" * 64,
                        config_sha256="b" * 64)
        self.sentinel = dict(schema=1, slot="B", version="1.1.0", manifest_sha256="c" * 64,
                             guard=make_ticket(config, self.record, previous))
        self.save()
        test = self

        class Platform:
            def __init__(self, config):
                self.config = config

            def inspect_early(self, record):
                test.assertEqual(record, test.record)
                test.events.append("inspect_without_data")
                return SimpleNamespace(active=test.active)

            def verify_good(self, layout, identity):
                test.events.append("verify_good")
                if test.good_error:
                    raise UpdateError("RECOVERY", "last-good identity invalid")

            def flush(self, path):
                test.events.append("flush_ticket")

            def quarantine_running(self):
                test.assertTrue(read_json(test.boot / "slot-valid.json")["guard"]["attempted"])
                test.events.append("quarantine")

            def write_pointers(self, layout, default, candidate):
                test.events.append(("pointer", default, candidate))

            def reboot(self):
                test.events.append("reboot")

        def sleep(seconds):
            self.time += seconds
        self.guard = EarlyGuard(boot_dir=self.boot, run_dir=self.run_dir,
                                platform_factory=Platform, clock=lambda: self.time, sleep=sleep)

    def save(self):
        atomic_write(self.boot / "slot-valid.json", canonical_json(self.sentinel), 0o644)

    def test_deadline_rolls_back_without_data_or_greetd(self):
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(self.guard.run(), "rollback_requested")
        self.assertEqual(self.events, ["inspect_without_data", "verify_good", "flush_ticket",
                                      "quarantine", ("pointer", "A", "A"), "reboot"])

    def test_default_ticket_uses_90_second_deadline(self):
        self.assertEqual(self.sentinel["guard"]["deadline_seconds"], 90)

    def test_new_and_legacy_tickets_roll_back_at_exactly_90_seconds(self):
        for seconds in (90, 600):
            with self.subTest(ticket_deadline=seconds):
                self.sentinel["guard"].update(deadline_seconds=seconds, attempted=False)
                self.save()
                self.events.clear()
                self.time = 89
                self.assertEqual(self.guard.run(), "rollback_requested")
                self.assertEqual(self.time, 90)
                saved = read_json(self.boot / "slot-valid.json")["guard"]
                self.assertEqual(saved["deadline_seconds"], seconds)
                self.assertTrue(saved["attempted"])
                self.assertEqual(self.events.count("reboot"), 1)

    def test_legacy_ticket_does_not_extend_deadline_on_late_start(self):
        self.sentinel["guard"]["deadline_seconds"] = 600
        self.save()
        self.time = 95
        self.guard.sleep = lambda _: self.fail("late legacy guard must roll back immediately")
        self.assertEqual(self.guard.run(), "rollback_requested")

    def test_restart_does_not_reset_boot_deadline(self):
        self.time = 900
        self.guard.sleep = lambda _: self.fail("overdue guard must not restart its timer")
        self.assertEqual(self.guard.run(), "rollback_requested")

    def test_missing_data_does_not_permit_second_rollback(self):
        self.guard.run()
        with self.assertRaisesRegex(UpdateError, "ROLLBACK_LOOP"):
            self.guard.run()
        self.assertEqual(self.events.count("reboot"), 1)

    def test_baseline_is_not_an_armed_candidate(self):
        self.sentinel.pop("guard")
        self.save()
        self.assertEqual(self.guard.run(), "baseline")
        self.assertEqual(self.events, [])

    def test_confirmed_marker_disarms_independent_timer(self):
        self.sentinel["guard"].update(armed=False, confirmed=True)
        self.save()
        self.assertEqual(self.guard.run(), "confirmed")
        self.assertEqual(self.events, [])

    def test_unarmed_unconfirmed_marker_is_not_success(self):
        self.sentinel["guard"]["armed"] = False
        self.save()
        with self.assertRaisesRegex(UpdateError, "BOOT_GUARD"):
            self.guard.run()
        self.assertEqual(self.events, [])

    def test_confirmation_while_waiting_exits_without_reboot(self):
        self.time = 20
        def confirm(seconds):
            self.time += seconds
            self.sentinel["guard"].update(armed=False, confirmed=True)
            self.save()
        self.guard.sleep = confirm
        self.assertEqual(self.guard.run(), "confirmed")
        self.assertEqual(self.events, [])

    def test_unverified_last_good_blocks_quarantine_and_reboot(self):
        self.good_error = True
        with self.assertRaisesRegex(UpdateError, "RECOVERY"):
            self.guard.run()
        self.assertFalse(read_json(self.boot / "slot-valid.json")["guard"]["attempted"])
        self.assertNotIn("quarantine", self.events)
        self.assertNotIn("reboot", self.events)

    def test_wrong_running_slot_never_quarantines_last_good(self):
        self.active = "A"
        with self.assertRaisesRegex(UpdateError, "BOOT_GUARD"):
            self.guard.run()
        self.assertNotIn("quarantine", self.events)

    def test_ticket_cannot_enable_missing_hardware_approval(self):
        self.sentinel["guard"]["approval"]["experimental_hardware_validation"] = False
        self.gate.stop()
        with self.assertRaisesRegex(UpdateError, "HARDWARE_GATE"):
            validate_ticket(self.sentinel, run_dir=self.run_dir)

    def test_ticket_has_no_arbitrary_paths_or_commands(self):
        self.sentinel["guard"]["approval"]["state_dir"] = "/arbitrary"
        with self.assertRaisesRegex(UpdateError, "BOOT_GUARD"):
            validate_ticket(self.sentinel, run_dir=self.run_dir)

    def test_deadline_policy_cannot_change_mid_wait(self):
        self.time = 20
        def change_policy(seconds):
            self.time += seconds
            self.sentinel["guard"]["deadline_seconds"] = 900
            self.save()
        self.guard.sleep = change_policy
        with self.assertRaisesRegex(UpdateError, "policy changed"):
            self.guard.run()
        self.assertNotIn("reboot", self.events)


if __name__ == "__main__":
    unittest.main()
