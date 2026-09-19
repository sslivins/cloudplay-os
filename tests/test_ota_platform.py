import copy
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from updater.platform import (BASIC, GEOMETRY, LABELS, LINUX, LinuxPlatform,
                              copy_payload, file_record, identity_files, safe_child,
                              run, validate_layout_record, validate_table, verify_slot)
from updater.state import Config, UpdateError, read_json


def table(disk="/dev/mmcblk0"):
    parts = []
    for number, (start, size) in enumerate(GEOMETRY, 1):
        parts.append(dict(node=f"{disk}p{number}", start=start * 2048,
                          size=(size or 11000) * 2048, name=LABELS[number - 1],
                          type=BASIC if number <= 3 else LINUX,
                          uuid=f"12345678-1234-1234-1234-{number:012d}"))
    return dict(label="gpt", sectorsize=512, device=disk, partitions=parts,
                id="98765432-1234-1234-1234-123456789abc")


class PlatformTests(unittest.TestCase):
    def _identity_fixture(self):
        if os.name == "posix" and os.getuid() != 0:
            self.skipTest("persistent identity ownership fixture requires Linux root")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        persistent, current = base / "identity", base / "etc"
        for directory in (persistent, current):
            (directory / "ssh").mkdir(parents=True)
            (directory / "machine-id").write_bytes(b"123456789abcdef0123456789abcdef0\n")
            for suffix, mode in (("", 0o600), (".pub", 0o644)):
                key = directory / "ssh" / ("ssh_host_rsa_key" + suffix)
                key.write_bytes(("fixture-" + suffix + "\n").encode())
                os.chmod(key, mode)
        # /tmp intentionally is writable; production rejects such ancestry.
        paths = patch("updater.platform.trusted_path")
        paths.start()
        self.addCleanup(paths.stop)
        return persistent, current

    def test_generated_identity_comes_from_persistent_seed(self):
        persistent, current = self._identity_fixture()
        files = identity_files(persistent, current)
        self.assertEqual(files["root/etc/machine-id"],
                         ((persistent / "machine-id").read_bytes(), 0o644))
        self.assertEqual(files["root/etc/ssh/ssh_host_rsa_key"][1], 0o600)
        self.assertNotIn("root/etc/ssh/ssh_host_ed25519_key", files)

    def test_persistent_machine_id_must_match_running_device(self):
        persistent, current = self._identity_fixture()
        (current / "machine-id").write_bytes(b"a" * 32 + b"\n")
        with self.assertRaisesRegex(UpdateError, "IDENTITY"):
            identity_files(persistent, current)

    def test_persistent_ssh_key_must_match_running_device(self):
        persistent, current = self._identity_fixture()
        (current / "ssh" / "ssh_host_rsa_key").write_bytes(b"different\n")
        with self.assertRaisesRegex(UpdateError, "IDENTITY"):
            identity_files(persistent, current)

    def test_unseeded_active_ssh_identity_is_not_silently_discarded(self):
        persistent, current = self._identity_fixture()
        (persistent / "ssh" / "ssh_host_rsa_key").unlink()
        (persistent / "ssh" / "ssh_host_rsa_key.pub").unlink()
        with self.assertRaisesRegex(UpdateError, "IDENTITY"):
            identity_files(persistent, current)

    def test_shared_layout_record_matches_exact_disk_and_partition_uuids(self):
        gpt = table()
        record = dict(schema=1, disk_id=gpt["id"],
                      partitions={part["name"]: part["uuid"] for part in gpt["partitions"]})
        validate_layout_record(record, gpt)
        for mutation in ("disk_id", "partition", "extra", "schema"):
            changed = copy.deepcopy(record)
            if mutation == "disk_id":
                changed["disk_id"] = "other-disk"
            elif mutation == "partition":
                changed["partitions"]["root-B"] = "other-partition"
            elif mutation == "extra":
                changed["extra"] = True
            else:
                changed["schema"] = True
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(UpdateError, "LAYOUT_RECORD"):
                    validate_layout_record(changed, gpt)

    def test_generated_candidate_arms_guard_and_leaves_data_mounting_to_firstboot(self):
        from updater.artifacts import GENERATED_PATHS
        from updater.boot import validate_ticket
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root, boot = base / "root", base / "boot"
            (root / "etc" / "ssh").mkdir(parents=True)
            (root / "usr/share/cloudplay").mkdir(parents=True)
            (root / "usr/share/cloudplay/kernel-command-line.txt").write_text(
                "console=tty3 root=PARTUUID=old ro quiet splash rootwait "
                "plymouth.ignore-serial-consoles init=/old-resize resize\n")
            boot.mkdir()
            layout = validate_table(table(), disk="/dev/mmcblk0", root_number=4, boot_number=2)
            record = dict(schema=1, disk_id=table()["id"],
                          partitions={part["name"]: part["uuid"] for part in table()["partitions"]})
            active = dict(schema=1, slot="A", version="1.0.0", manifest_sha256="a" * 64)
            config = replace(Config(), experimental_hardware_validation=True,
                             hardware_evidence="physical approval", launcher_isolation_verified=True,
                             isolation_evidence="UID split approval")
            platform = LinuxPlatform(config)
            def read_source(path, *args):
                return active if path.name == "slot-valid.json" else record
            with patch.object(Config, "mutation_gate"), \
                    patch("updater.platform.read_json", side_effect=read_source), \
                    patch("updater.platform.sha256_file", return_value="b" * 64), \
                    patch("updater.platform.identity_files", return_value={
                        "root/etc/machine-id": (b"a" * 32 + b"\n", 0o644)}), \
                    patch("updater.platform._set_attributes") as attributes, \
                    patch("updater.platform.GENERATED_PATHS",
                          GENERATED_PATHS | {"boot/autoboot.txt"}):
                generated = platform._generated(
                    layout, dict(version="1.1.0", manifest_sha256="c" * 64,
                                 manifest={"root/usr/share/cloudplay/kernel-command-line.txt": {
                                     "type": "file", "sha256": "b" * 64}}), root, boot)
                sentinel = read_json(boot / "slot-valid.json")
                ticket, _ = validate_ticket(sentinel, run_dir=base / "run")
                self.assertTrue(ticket["armed"])
                self.assertTrue(ticket["approval"]["experimental_hardware_validation"])
                self.assertEqual(ticket["previous"]["config_sha256"], "b" * 64)
                self.assertEqual(ticket["layout"], record)
                self.assertEqual(attributes.call_count, 2)
                for call in attributes.call_args_list:
                    self.assertEqual(call.args[1]["uid"], 0)
                    self.assertEqual(call.args[1]["gid"], 0)
            self.assertIn("boot/slot-valid.json", generated)
            fstab = (root / "etc/fstab").read_text()
            self.assertIn("PARTUUID=" + layout.part(5).uuid, fstab)
            self.assertIn("PARTUUID=" + layout.part(3).uuid, fstab)
            self.assertNotIn("/data", fstab)
            self.assertNotIn("profiles", fstab)
            command_line = (boot / "cmdline.txt").read_text()
            self.assertIn("console=tty3", command_line)
            self.assertIn("splash", command_line)
            self.assertIn("plymouth.ignore-serial-consoles", command_line)
            self.assertIn("root=PARTUUID=" + layout.part(5).uuid, command_line)
            self.assertNotIn("old", command_line)
            self.assertNotIn("resize", command_line)

    def test_fat_first_layout_both_directions(self):
        for root, boot, active in ((4, 2, "A"), (5, 3, "B")):
            layout = validate_table(table(), disk="/dev/mmcblk0",
                                    root_number=root, boot_number=boot)
            self.assertEqual(layout.active, active)
            self.assertEqual(layout.part(1).size, 64 * 1024**2)
            self.assertEqual(layout.part(4).size, 8 * 1024**3)

    def test_bad_geometry_uuid_type_disk_rejected(self):
        changes = (("start", 999), ("size", 999), ("uuid", "bad"),
                   ("type", LINUX), ("name", "boot-B"))
        for key, value in changes:
            with self.subTest(key=key):
                bad = table()
                bad["partitions"][0][key] = value
                with self.assertRaisesRegex(UpdateError, "LAYOUT"):
                    validate_table(bad, disk="/dev/mmcblk0", root_number=4, boot_number=2)
        with self.assertRaisesRegex(UpdateError, "LAYOUT"):
            validate_table(table(), disk="/dev/nvme0n1", root_number=4, boot_number=2)

    def test_mixed_active_root_boot_rejected(self):
        with self.assertRaisesRegex(UpdateError, "LAYOUT"):
            validate_table(table(), disk="/dev/mmcblk0", root_number=4, boot_number=3)

    def test_nvme_root_sd_boot_is_not_resolved_by_duplicate_labels(self):
        platform = LinuxPlatform(Config(), runner=Mock())
        platform.mount_info = Mock(side_effect=[
            {"fstype": "ext4", "maj:min": "259:4"},
            {"fstype": "vfat", "maj:min": "179:2"},
            {"fstype": "ext4", "maj:min": "259:6"}])
        platform.ancestry = Mock(side_effect=[
            ("/dev/nvme0n1", 4), ("/dev/mmcblk0", 2), ("/dev/nvme0n1", 6)])
        with self.assertRaisesRegex(UpdateError, "same physical disk"):
            platform.inspect()
        platform.run.assert_not_called()

    def test_subprocess_deadline_failure_is_typed(self):
        with patch("updater.platform.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("findmnt", 30)) as command:
            with self.assertRaisesRegex(UpdateError, "COMMAND"):
                run(["findmnt", "--json"])
        self.assertEqual(command.call_args.kwargs["timeout"], 30)
        self.assertNotIn("shell", command.call_args.kwargs)

    def test_active_provider_cannot_hide_in_description(self):
        runner = Mock(return_value=SimpleNamespace(
            stdout="cloudplay-stream.service loaded active running inactive provider\n"))
        platform = LinuxPlatform(Config(), runner=runner)
        with self.assertRaisesRegex(UpdateError, "PROVIDER_ACTIVE"):
            platform.provider_idle()
        self.assertEqual(runner.call_count, 1)

    def test_no_destructive_runner_called_with_default_gate(self):
        runner = Mock()
        platform = LinuxPlatform(Config(), runner=runner)
        with self.assertRaisesRegex(UpdateError, "HARDWARE_GATE"):
            platform.mutate(["mkfs.ext4", "/dev/fake"])
        runner.assert_not_called()

    def test_generated_paths_reject_traversal(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(UpdateError, "PATH"):
                safe_child(Path(name), "../host")

    def test_generated_paths_do_not_follow_payload_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "host").mkdir()
            (root / "payload").mkdir()
            try:
                os.symlink(root / "host", root / "payload" / "etc", target_is_directory=True)
            except OSError:
                self.skipTest("symlink privilege unavailable")
            with self.assertRaisesRegex(UpdateError, "PATH"):
                safe_child(root / "payload", "etc/fstab")
            self.assertFalse((root / "host" / "fstab").exists())

    def test_copy_absolute_root_link_does_not_follow_it(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, target = base / "source", base / "target"
            (source / "root" / "usr").mkdir(parents=True)
            (source / "root" / "usr" / "payload").write_bytes(b"safe")
            target.mkdir()
            uid = os.getuid() if os.name == "posix" else 0
            gid = os.getgid() if os.name == "posix" else 0
            directory = dict(type="directory", mode=0o755, uid=uid, gid=gid)
            metadata = {"manifest": {
                "root": directory, "root/usr": directory,
                "root/usr/payload": file_record(b"safe", uid=uid, gid=gid),
                "root/bin": dict(type="symlink", target="/usr", uid=uid, gid=gid, mode=0o777),
            }}
            try:
                copy_payload(source, target, metadata, "root")
            except OSError as exc:
                if os.name != "posix":
                    self.skipTest(f"symlink privilege unavailable: {exc}")
                raise
            self.assertEqual(os.readlink(target / "bin"), "/usr")
            self.assertEqual((target / "usr" / "payload").read_bytes(), b"safe")
            if os.name == "posix":
                self.assertEqual((target / "usr" / "payload").stat().st_uid, uid)
                self.assertEqual((target / "usr" / "payload").stat().st_mode & 0o7777, 0o644)

    def test_copy_progress_counts_bytes_excludes_withheld_and_finishes_after_sync(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, target = base / "source", base / "target"
            (source / "boot").mkdir(parents=True)
            target.mkdir()
            content = b"x" * (2 * 1024**2 + 9)
            (source / "boot/payload").write_bytes(content)
            (source / "boot/config.txt").write_bytes(b"withheld")
            metadata = {"manifest": {"boot/payload": file_record(content),
                                      "boot/config.txt": file_record(b"withheld")}}
            events = []
            with patch("updater.platform.os.fsync", side_effect=lambda _: events.append("sync")):
                copy_payload(source, target, metadata, "boot", frozenset({"boot/config.txt"}),
                             progress=lambda received, total: events.append((received, total)))
            self.assertEqual(events, [(0, len(content)), (1024**2, len(content)),
                                      (2 * 1024**2, len(content)), "sync",
                                      (len(content), len(content))])
            self.assertEqual((target / "payload").read_bytes(), content)
            self.assertFalse((target / "config.txt").exists())

    def test_copy_failure_does_not_report_completion(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, target = base / "source", base / "target"
            (source / "boot").mkdir(parents=True)
            target.mkdir()
            (source / "boot/payload").write_bytes(b"payload")
            progress = Mock()
            with patch("updater.platform.os.fsync", side_effect=OSError("injected write failure")):
                with self.assertRaisesRegex(OSError, "injected write failure"):
                    copy_payload(source, target, {"manifest": {"boot/payload": file_record(b"payload")}},
                                 "boot", progress=progress)
            self.assertEqual(progress.call_args_list, [unittest.mock.call(0, 7)])

    @unittest.skipUnless(os.name == "posix", "POSIX root filenames require native Linux storage")
    def test_root_dpkg_colons_and_case_variants_survive_physical_copy(self):
        import hashlib
        from updater.artifacts import canonical_json, verify_tree
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, root, boot = base / "source", base / "root", base / "boot"
            source.mkdir()
            root.mkdir()
            boot.mkdir()
            uid, gid = os.getuid(), os.getgid()
            directory = dict(type="directory", mode=0o755, uid=uid, gid=gid)
            manifest = {}
            for relative in ("boot", "root", "root/var", "root/var/lib",
                             "root/var/lib/dpkg", "root/var/lib/dpkg/info"):
                path = source / relative
                path.mkdir()
                os.chmod(path, 0o755)
                manifest[relative] = dict(directory)
            for filename, content in (("libc6:arm64.list", b"/usr/lib/libc.so.6\n"),
                                      ("Package", b"upper\n"), ("package", b"lower\n")):
                relative = "root/var/lib/dpkg/info/" + filename
                path = source / relative
                path.write_bytes(content)
                os.chmod(path, 0o644)
                manifest[relative] = file_record(content, uid=uid, gid=gid)
            metadata = dict(manifest=manifest,
                            manifest_sha256=hashlib.sha256(canonical_json(manifest)).hexdigest())
            verify_tree(source, metadata)
            copy_payload(source, root, metadata, "root")
            verify_slot(root, boot, metadata, {})
            self.assertEqual((root / "var/lib/dpkg/info/libc6:arm64.list").read_bytes(),
                             b"/usr/lib/libc.so.6\n")
            self.assertNotEqual((root / "var/lib/dpkg/info/Package").read_bytes(),
                                (root / "var/lib/dpkg/info/package").read_bytes())

    def test_closed_generated_manifest_verification(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root, boot = base / "root", base / "boot"
            root.mkdir()
            boot.mkdir()
            os.chmod(root, 0o755)
            uid = os.getuid() if os.name == "posix" else 0
            gid = os.getgid() if os.name == "posix" else 0
            directory = dict(type="directory", mode=0o755, uid=uid, gid=gid)
            meta = {"manifest": {"root": directory, "boot": directory}}
            (boot / "cmdline.txt").write_bytes(b"root=PARTUUID=abc\n")
            generated = {"boot/cmdline.txt": file_record(b"root=PARTUUID=abc\n")}
            samples = []
            verify_slot(root, boot, meta, generated,
                        activity=lambda *event: samples.append(event), operation="check_restart")
            size = len(b"root=PARTUUID=abc\n")
            self.assertEqual(samples, [("check_restart", 0, size), ("check_restart", size, size)])
            (boot / "surprise").write_bytes(b"x")
            with self.assertRaisesRegex(UpdateError, "SLOT_VERIFY"):
                verify_slot(root, boot, meta, generated)
            with self.assertRaisesRegex(UpdateError, "GENERATED"):
                verify_slot(root, boot, meta, {"root/etc/arbitrary": file_record(b"x")})

    def test_physical_readback_delegates_root_attributes_only(self):
        with tempfile.TemporaryDirectory() as name:
            root, boot = Path(name) / "root", Path(name) / "boot"
            root.mkdir()
            boot.mkdir()
            record = dict(type="directory", mode=0o755, uid=0, gid=0,
                          xattrs={"user.test": "dmFsdWU="})
            metadata = {"manifest": {
                "root": record,
                "boot": dict(type="directory", mode=0o755, uid=0, gid=0)}}
            with patch("updater.platform.verify_attributes") as attributes:
                verify_slot(root, boot, metadata, {})
            attributes.assert_called_once_with(root, record)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "native Linux root required for ACL/capability preservation")
    def test_root_acls_capabilities_and_user_xattrs_survive_copy_and_readback(self):
        import hashlib
        import struct
        from updater.artifacts import (ArtifactError, canonical_json, read_xattrs,
                                       verify_tree)
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, root, boot = base / "source", base / "root", base / "boot"
            for path in (source, source / "root", source / "boot",
                         source / "root" / "journal", root, boot):
                path.mkdir()
                os.chmod(path, 0o755)
            journal = source / "root" / "journal"
            acl = struct.pack("<I", 2) + b"".join(
                struct.pack("<HHI", tag, permissions, identifier)
                for tag, permissions, identifier in (
                    (1, 7, 0xffffffff), (2, 4, 12345), (4, 5, 0xffffffff),
                    (16, 5, 0xffffffff), (32, 5, 0xffffffff)))
            os.setxattr(journal, "system.posix_acl_access", acl)
            os.setxattr(journal, "system.posix_acl_default", acl)
            os.setxattr(journal, "user.cloudplay", b"signed")
            executable = source / "root" / "program"
            payload = b"fixture only; never executed\n"
            executable.write_bytes(payload)
            os.chmod(executable, 0o755)
            capability = struct.pack("<IIIII", 0x02000001, 1 << 13, 0, 0, 0)
            os.setxattr(executable, "security.capability", capability)
            directory = dict(type="directory", mode=0o755, uid=0, gid=0)
            manifest = {
                "root": dict(directory), "boot": dict(directory),
                "root/journal": dict(directory, xattrs=read_xattrs(journal)),
                "root/program": dict(file_record(payload, mode=0o755),
                                     xattrs=read_xattrs(executable)),
            }
            metadata = dict(manifest=manifest,
                            manifest_sha256=hashlib.sha256(canonical_json(manifest)).hexdigest())
            verify_tree(source, metadata)
            copy_payload(source, root, metadata, "root")
            verify_slot(root, boot, metadata, {})
            self.assertEqual(read_xattrs(root / "journal"), read_xattrs(journal))
            self.assertEqual(read_xattrs(root / "program"), read_xattrs(executable))
            for relative, key, changed in (
                    ("journal", "user.cloudplay", b"tampered"),
                    ("journal", "system.posix_acl_default", None),
                    ("program", "security.capability", None),
                    ("program", "user.unsigned", b"unexpected")):
                with self.subTest(relative=relative, key=key):
                    target = root / relative
                    original = os.getxattr(target, key) if key in os.listxattr(target) else None
                    if changed is None:
                        os.removexattr(target, key)
                    else:
                        os.setxattr(target, key, changed)
                    with self.assertRaisesRegex(ArtifactError, "ATTRIBUTES"):
                        verify_slot(root, boot, metadata, {})
                    if original is None:
                        os.removexattr(target, key)
                    else:
                        os.setxattr(target, key, original)
                    verify_slot(root, boot, metadata, {})

    def test_pointer_rewrites_both_all_and_tryboot(self):
        self.assertEqual(LinuxPlatform.pointer("B", "A"),
                         b"[all]\ntryboot_a_b=1\nboot_partition=3\n[tryboot]\nboot_partition=2\n")

    def test_physical_stage_gates_firmware_before_root_and_publishes_last(self):
        self._stage_fixture()

    def test_power_loss_at_candidate_journal_keeps_config_withheld(self):
        self._stage_fixture(fail_publish=True)

    def _stage_fixture(self, *, fail_publish=False):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, target_boot, target_root = base / "source", base / "boot", base / "root"
            for path in (source, source / "boot", source / "root", target_boot, target_root):
                path.mkdir()
                os.chmod(path, 0o755)
            content = b"arm_64bit=1\n"
            (source / "boot" / "config.txt").write_bytes(content)
            os.chmod(source / "boot" / "config.txt", 0o644)
            for filename in ("config.txt", "tryboot.txt"):
                (target_boot / filename).write_bytes(b"old")
            uid = os.getuid() if os.name == "posix" else 0
            gid = os.getgid() if os.name == "posix" else 0
            directory = dict(type="directory", mode=0o755, uid=0, gid=0)
            root_directory = dict(type="directory", mode=0o755, uid=uid, gid=gid)
            manifest = {"boot": directory, "root": root_directory,
                        "boot/config.txt": file_record(content)}
            for relative in ("usr", "usr/share", "usr/share/cloudplay"):
                (source / "root" / relative).mkdir()
                os.chmod(source / "root" / relative, 0o755)
                manifest["root/" + relative] = root_directory
            template = b"console=tty3 quiet splash rootwait\n"
            (source / "root/usr/share/cloudplay/kernel-command-line.txt").write_bytes(template)
            os.chmod(source / "root/usr/share/cloudplay/kernel-command-line.txt", 0o644)
            manifest["root/usr/share/cloudplay/kernel-command-line.txt"] = file_record(template, uid=uid, gid=gid)
            import hashlib
            from updater.artifacts import canonical_json
            metadata = dict(manifest=manifest,
                            manifest_sha256=hashlib.sha256(canonical_json(manifest)).hexdigest())
            events = []
            layout = validate_table(table(), disk="/dev/mmcblk0", root_number=4, boot_number=2)

            class FixturePlatform(LinuxPlatform):
                def inspect(self):
                    return layout

                def provider_idle(self):
                    pass

                def unmounted(self, part):
                    events.append(("unmounted", part.number))

                @contextmanager
                def mounted(self, part, label):
                    self.assert_inactive(part)
                    yield target_boot if part.number == 3 else target_root

                def assert_inactive(self, part):
                    if part.number not in (3, 5):
                        raise AssertionError("active partition selected")

                def flush(self, path):
                    events.append(("flush", path.name))
                    if path == target_boot and ("mkfs.vfat",) not in events:
                        if any((path / p).exists() for p in ("config.txt", "tryboot.txt")):
                            raise AssertionError("firmware gate not removed before initial flush")

                def snapshot_profiles(self, layout):
                    events.append(("profiles",))

                def mutate(self, args, *, timeout=60):
                    self.config.mutation_gate()
                    events.append((args[0],))
                    if args[0] == "mkfs.ext4":
                        if (target_boot / "config.txt").exists() or (target_boot / "tryboot.txt").exists():
                            raise AssertionError("root write with bootable target")

                def _generated(self, *args):
                    return {}

            platform = FixturePlatform(Config())
            phases = []
            progress = Mock()
            def checkpoint(phase, generated):
                phases.append(phase)
                if phase == "publishing":
                    self.assertFalse((target_boot / "config.txt").exists())
                    if fail_publish:
                        raise UpdateError("POWER_LOSS", "injected after verification")
                if phase == "ready_to_restart":
                    self.assertEqual((target_boot / "config.txt").read_bytes(), content)
            with patch.object(Config, "mutation_gate"):
                # Source numeric FAT ownership is root in production. Windows
                # cannot attest it; Linux runs use actual root-owned fixtures.
                if os.name == "posix" and os.getuid() != 0:
                    self.skipTest("physical staging fixture needs numeric root ownership")
                if fail_publish:
                    with self.assertRaisesRegex(UpdateError, "POWER_LOSS"):
                        platform.stage(source, metadata, layout, checkpoint)
                    self.assertFalse((target_boot / "config.txt").exists())
                else:
                    platform.stage(source, metadata, layout, checkpoint, progress=progress)
                    self.assertEqual(phases[-2:], ["publishing", "ready_to_restart"])
                    self.assertIn(unittest.mock.call("staging_boot", 0, 0), progress.call_args_list)
                    self.assertIn(unittest.mock.call("staging_root", 0, len(template)), progress.call_args_list)
                    self.assertEqual(progress.call_args.args, ("staging_root", len(template), len(template)))
            self.assertLess(events.index(("flush", "boot")), events.index(("mkfs.ext4",)))
            self.assertLess(events.index(("unmounted", 5)), events.index(("mkfs.ext4",)))


if __name__ == "__main__":
    unittest.main()
