import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ota_layout", ROOT / "image-build/layout.py")
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)
sys.modules["layout"] = layout
firstboot_spec = importlib.util.spec_from_file_location(
    "ota_firstboot", ROOT / "image-build/firstboot.py")
firstboot = importlib.util.module_from_spec(firstboot_spec)
firstboot_spec.loader.exec_module(firstboot)
configure_spec = importlib.util.spec_from_file_location(
    "ota_configuration", ROOT / "image-build/configure.py")
configure = importlib.util.module_from_spec(configure_spec)
configure_spec.loader.exec_module(configure)
boot_spec = importlib.util.spec_from_file_location(
    "ota_boot_service", ROOT / "image-build/boot-service.py")
boot_service = importlib.util.module_from_spec(boot_spec)
boot_spec.loader.exec_module(boot_service)
assemble_spec = importlib.util.spec_from_file_location("ota_assembler", ROOT / "image-build/assemble.py")
assembler = importlib.util.module_from_spec(assemble_spec)
assemble_spec.loader.exec_module(assembler)


def table():
    return {
        "label": "gpt", "sectorsize": 512,
        "partitions": [
            {"name": label, "start": start * 2048, "size": size * 2048,
             "node": "/dev/mmcblk0p" + str(number),
             "uuid": str(uuid.uuid4()),
             "type": ("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7" if number <= 3
                      else "0FC63DAF-8483-4772-8E79-3D69D8477DE4")}
            for number, label, _, _, start, size in layout.PARTITIONS
        ],
    }


class GeometryTests(unittest.TestCase):
    def test_assembler_refuses_shared_device_identity(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            boot, root = base / "boot", base / "root"
            boot.mkdir()
            (root / "etc/ssh").mkdir(parents=True)
            (boot / "cmdline.txt").write_text("rootwait quiet splash\n")
            self.assertEqual(assembler.validate_inputs(boot, root), "rootwait quiet splash\n")
            (root / "etc/ssh/ssh_host_ed25519_key").write_text("not-a-real-key")
            with self.assertRaisesRegex(ValueError, "shared SSH host key"):
                assembler.validate_inputs(boot, root)

    @unittest.skipUnless(os.name == "posix", "requires symlinks")
    def test_assembler_generated_paths_cannot_follow_host_links(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            boot, root = base / "boot", base / "root"
            boot.mkdir()
            (root / "etc/ssh").mkdir(parents=True)
            (boot / "cmdline.txt").write_text("quiet\n")
            protected = base / "host-fstab"
            protected.write_text("preserve")
            (root / "etc/fstab").symlink_to(protected)
            with self.assertRaises(ValueError):
                assembler.validate_inputs(boot, root)
            self.assertEqual(protected.read_text(), "preserve")

    @unittest.skipUnless(os.name == "posix", "image configuration targets Linux paths")
    def test_generated_config_does_not_claim_browser_isolation(self):
        value = configure.configuration(
            {"evidence": "measured fixture only", "platforms": {
                "cm5": {"minimum_eeprom": "2026-02-23", "boot_order": "0xf2461"}}}, "cm5", 1)
        self.assertFalse(value["launcher_isolation_verified"])
        self.assertFalse(value["experimental_hardware_validation"])
        self.assertEqual(value["launcher_uid"], value["browser_uid"])
        approved_flags = value | {"launcher_isolation_verified": True,
                                  "experimental_hardware_validation": True,
                                  "isolation_evidence": "not an actual boundary"}
        with self.assertRaisesRegex(boot_service.UpdateError, "ISOLATION_GATE"):
            configure.Config(**approved_flags).mutation_gate()

    def test_discovery_bootstrap_never_bypasses_candidate_gate(self):
        runtime = mock.Mock()
        runtime.config = configure.Config(browser_uid=1000)
        with tempfile.TemporaryDirectory() as name:
            runtime.journal.path = Path(name) / "absent.json"
            runtime.journal.load.return_value = {"pending": None}
            result = boot_service.execute("bootstrap", runtime)
            self.assertTrue(result["mutation_disabled"])
            runtime.initialize.assert_called_once()
            runtime.reconcile.assert_not_called()
            runtime.journal.load.return_value = {"pending": {"slot": "B"}}
            with self.assertRaisesRegex(boot_service.UpdateError, "RECOVERY_REQUIRED"):
                boot_service.execute("health", runtime)

    def test_service_restart_is_not_graceful_system_shutdown(self):
        runtime = mock.Mock()
        with mock.patch.object(boot_service.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="running\n")
            result = boot_service.execute("shutdown", runtime)
        self.assertFalse(result["graceful_shutdown_recorded"])
        runtime.shutdown.assert_not_called()
        runtime.journal.load.assert_not_called()

    def test_firstboot_requires_matching_same_disk_pair(self):
        value = table()
        self.assertEqual(firstboot.identity(
            value, "/dev/mmcblk0p4", "/dev/mmcblk0p2", 31914983424)[0], "A")
        self.assertEqual(firstboot.identity(
            value, "/dev/mmcblk0p5", "/dev/mmcblk0p3", 31914983424)[0], "B")
        for root, boot, capacity in (
                ("/dev/nvme0n1p2", "/dev/mmcblk0p2", 31914983424),
                ("/dev/mmcblk0p4", "/dev/mmcblk0p3", 31914983424),
                ("/dev/mmcblk0p4", "/dev/mmcblk0p2", 16_000_000_000)):
            with self.subTest(root=root, boot=boot, capacity=capacity), self.assertRaises(ValueError):
                firstboot.identity(value, root, boot, capacity)

    def test_fat_first_and_nonoverlapping(self):
        self.assertEqual([row[1] for row in layout.PARTITIONS],
                         ["boot-control", "boot-A", "boot-B", "root-A", "root-B", "data"])
        previous_end = 0
        for _, _, _, _, start, size in layout.PARTITIONS:
            self.assertEqual(start % 4, 0)
            self.assertGreaterEqual(start, previous_end)
            previous_end = start + size
        self.assertLess(previous_end * layout.MIB, layout.IMAGE_BYTES)
        self.assertLess(layout.IMAGE_BYTES, layout.MIN_CARD_BYTES)
        self.assertLessEqual(layout.MIN_CARD_BYTES, 31914983424)
        layout.validate_geometry(table())

    def test_only_data_can_grow(self):
        value = table()
        value["partitions"][-1]["size"] += 8192
        layout.validate_geometry(value, growing=True)
        with self.assertRaises(ValueError):
            layout.validate_geometry(value)
        for index in range(5):
            changed = table()
            changed["partitions"][index]["size"] += 8192
            with self.subTest(index=index), self.assertRaises(ValueError):
                layout.validate_geometry(changed, growing=True)

    def test_rejects_wrong_type_missing_uuid_and_interleaved_layout(self):
        for field, bad in (("type", "8300"), ("uuid", ""), ("start", 0), ("name", "root-B")):
            value = table()
            value["partitions"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                layout.validate_geometry(value)
        value = table()
        value["partitions"][2], value["partitions"][3] = value["partitions"][3], value["partitions"][2]
        with self.assertRaises(ValueError):
            layout.validate_geometry(value)
        value = table()
        value["partitions"][1]["uuid"] = value["partitions"][0]["uuid"]
        with self.assertRaises(ValueError):
            layout.validate_geometry(value)

    def test_generated_boot_identity_uses_unique_partuuid(self):
        root, boot = str(uuid.uuid4()), str(uuid.uuid4())
        result = layout.fstab("B", root, boot)
        self.assertIn("PARTUUID=" + root, result)
        self.assertIn("PARTUUID=" + boot, result)
        self.assertNotIn("PARTLABEL", result)
        command = layout.cmdline("root=PARTUUID=old resize ro quiet init=/bad systemd.run=/bad", "B", root)
        self.assertEqual(command, f"quiet root=PARTUUID={root} rw\n")
        for bad in ("", "../../mmcblk0", "a b", None):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                layout.fstab("A", bad, boot)

    def test_approval_does_not_claim_production(self):
        preflight_spec = importlib.util.spec_from_file_location(
            "ota_preflight", ROOT / "image-build/preflight.py")
        preflight = importlib.util.module_from_spec(preflight_spec)
        preflight_spec.loader.exec_module(preflight)
        approval = dict(schema=1, experimental_build_approved=True,
                        production_approved=False, physical_acceptance="incomplete",
                        evidence="hardware fixture", reviewed_by="operator",
                        platforms={"cm5": dict(minimum_eeprom="2026-02-23",
                                               boot_order="0xf2461", tryboot_required=True)})
        preflight.validate_approval(approval)
        with self.assertRaises(ValueError):
            preflight.validate_approval(approval | {"production_approved": True})


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "requires a root-owned POSIX fixture")
class FirstbootFilesystemTests(unittest.TestCase):
    def test_atomic_identity_and_layout_binding(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-firstboot-") as name:
            work = Path(name)
            value = table() | {"id": str(uuid.uuid4())}
            saved = work / "layout.json"
            content = json.dumps({"schema": 1, "disk_id": value["id"],
                                  "partitions": {p["name"]: p["uuid"] for p in value["partitions"]}})
            firstboot.atomic(saved, content.encode(), 0o600)
            firstboot.validate_saved_layout(saved, value, value["partitions"])
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "does not match"):
                firstboot.validate_saved_layout(saved, value | {"id": "different"}, value["partitions"])
            saved.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                firstboot.validate_saved_layout(saved, value, value["partitions"])

    def test_identity_writes_never_follow_links(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-firstboot-") as name:
            work = Path(name)
            outside = work / "keep"
            outside.write_bytes(b"preserve")
            link = work / "identity"
            link.symlink_to(outside)
            with self.assertRaises(ValueError):
                firstboot.atomic(link, b"overwrite")
            self.assertEqual(outside.read_bytes(), b"preserve")
            link.unlink()
            (work / ".identity.new").symlink_to(outside)
            with self.assertRaises(OSError):
                firstboot.atomic(link, b"overwrite")
            self.assertEqual(outside.read_bytes(), b"preserve")

    def test_directory_ownership_and_seed_are_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-firstboot-") as name:
            work = Path(name)
            source, target = work / "source", work / "persistent"
            source.mkdir()
            (source / "state").write_text("initial")
            firstboot.seed_directory(source, target)
            (target / "state").write_text("saved")
            (source / "state").write_text("new image default")
            firstboot.seed_directory(source, target)
            self.assertEqual((target / "state").read_text(), "saved")
            os.chown(target, 1000, 1000)
            with self.assertRaises(ValueError):
                firstboot.directory(target)


if __name__ == "__main__":
    unittest.main()
