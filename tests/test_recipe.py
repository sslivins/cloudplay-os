import copy
import hashlib
import importlib.util
import io
import json
import re
import shutil
import stat
import struct
import tarfile
import tomllib
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifacts = load("artifacts", "artifacts.py")
installer = load("installer", "install-extension.py")
hdr = load("hdr", "hdr-readiness.py")
supervisor = load("supervisor", "supervise.py")
verifier = load("verifier", "verify-kiosk.py")


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

    def test_versioned_browser_release_tags(self):
        for tag in ("v0.4.1", "chromium-153.0.8010.47-2-rpt1-hevc1"):
            value = self.finalized()
            value["browser"]["release"] = tag
            with self.subTest(tag=tag):
                artifacts.validate(value)

    def test_floating_or_unsafe_browser_release_tags_rejected(self):
        for tag in ("main", "latest", "v0.4", "../v0.4.1", "v0.4.1/other",
                    "chromium-153", "chromium-153.0.8010.47-2-rpt1",
                    "chromium-153.0.8010.47-2-rpt1-hevc1?download=1",
                    "chromium-153.0.8010.47-2-rpt1-hevc1\n"):
            value = self.finalized()
            value["browser"]["release"] = tag
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                artifacts.validate(value)

    def test_package_version_is_distinct_from_asset_filename_version(self):
        value = self.finalized()
        self.assertEqual(value["browser"]["package_version"], "1:153.0.8010.47-2~deb13u1+rpt1")
        self.assertNotEqual(value["browser"]["package_version"], value["browser"]["version"])
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn('lock["browser"]["package_version"]', stage)
        value["browser"]["package_version"] = "bad version"
        with self.assertRaises(ValueError):
            artifacts.validate(value)

    def test_export_report_uses_browser_manifest_pins(self):
        source = (ROOT / "scripts/verify-kiosk.py").read_text()
        self.assertIn('Path("/opt/cloudplay-build-inputs/manifest.json").read_text()', source)
        self.assertIn('"browser_release": browser["release"]', source)
        self.assertIn('"browser_package_version": browser["package_version"]', source)

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
    def test_browser_password_saving_is_disabled_by_managed_policy(self):
        policy_file = ROOT / "stage-cloudplay/00-appliance/files/cloudplay-browser-policy.json"
        policy = json.loads(policy_file.read_text())
        self.assertIs(policy["PasswordManagerEnabled"], False)
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn('install -m 644 files/cloudplay-browser-policy.json '
                      '"${ROOTFS_DIR}/etc/chromium/policies/managed/cloudplay.json"', stage)
        path = Mock()
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
        path.read_text.return_value = policy_file.read_text()
        self.assertEqual(verifier.verify_browser_policy(path), {
            "new_password_saving": False,
            "clipboard_allowed_origins": ["https://play.geforcenow.com"]})
        for value in (True, None, 0, "false"):
            path.read_text.return_value = json.dumps({"PasswordManagerEnabled": value})
            with self.subTest(value=value), self.assertRaises(AssertionError):
                verifier.verify_browser_policy(path)
        for origins in (None, [], ["*"], ["https://[*.]geforcenow.com"],
                        ["http://play.geforcenow.com"],
                        ["https://play.geforcenow.com", "https://www.xbox.com"]):
            path.read_text.return_value = json.dumps({
                "PasswordManagerEnabled": False, "ClipboardAllowedForUrls": origins})
            with self.subTest(origins=origins), self.assertRaises(AssertionError):
                verifier.verify_browser_policy(path)
        path.read_text.return_value = policy_file.read_text()
        for mode, uid in ((stat.S_IFREG | 0o666, 0), (stat.S_IFREG | 0o644, 1000),
                          (stat.S_IFLNK | 0o777, 0)):
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
            with self.subTest(mode=mode, uid=uid), self.assertRaises(AssertionError):
                verifier.verify_browser_policy(path)

    def test_locked_kiosk_and_no_stock_wizard(self):
        config = (ROOT / "config").read_text()
        for expected in ("FIRST_USER_PASS=''", "DISABLE_FIRST_BOOT_USER_RENAME=0",
                         "ENABLE_SSH=0", "PASSWORDLESS_SUDO=0", "FIRST_USER_NAME='cloudplay'"):
            self.assertIn(expected, config)
        self.assertIn("stage2 stage-cloudplay", config)
        self.assertNotIn("stage3", config)
        build = (ROOT / "scripts/build-image.sh").read_text()
        self.assertIn("rm -rf build/pi-gen/export-image/01-user-rename", build)
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn("apt-get purge -y userconf-pi rpi-connect-lite", stage)
        self.assertIn("usermod --password '*' --shell /bin/bash --groups audio,video,render,cloudplay-gamepad cloudplay", stage)
        self.assertNotIn("do_boot_behaviour B4", stage)
        self.assertIn("systemctl mask ssh.service", stage)
        self.assertIn("export CLOUDPLAY_DEVELOPMENT_SSH=1", config)
        self.assertIn('/usr/local/bin/cloudplay-development-ssh "$(cat /etc/cloudplay/development-ssh)"', stage)

    def test_preview_ssh_is_separate_password_authenticated_admin_not_browser(self):
        script = (ROOT / "stage-cloudplay/00-appliance/files/cloudplay-development-ssh").read_text()
        for required in ("PermitRootLogin no", "DenyUsers root cloudplay",
                         "AuthenticationMethods any", "PermitEmptyPasswords no",
                         "cloud ALL=(ALL:ALL) PASSWD: ALL", "printf 'cloud:cloud\\n' | chpasswd",
                         '[[ "$(id -u cloud)" -gt 1000 ]]', "usermod --lock --shell /usr/sbin/nologin cloud",
                         "systemctl mask ssh.service ssh.socket"):
            self.assertIn(required, script)
        self.assertNotIn("NOPASSWD", script)
        self.assertNotIn("usermod --append --groups sudo cloudplay", script)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("systemctl start", script)
        enabled = script.index('if [[ "$enabled" == 1 ]]')
        self.assertGreater(script.index("useradd --create-home"), enabled)
        self.assertIn("ssh_pwauth: ${value}", script)
        packages = (ROOT / "stage-cloudplay/00-appliance/00-packages-nr").read_text().split()
        self.assertTrue({"openssh-server", "sudo"} <= set(packages))
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn("rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub", stage)
        self.assertIn("systemctl enable regenerate_ssh_host_keys.service", stage)

    def test_image_staging_includes_new_boot_gate_modules(self):
        build = (ROOT / "scripts/build-image.sh").read_text()
        self.assertIn("client.py,readiness.py,boot.py,setup.html", build)

    def test_ota_does_not_automatically_change_bootloader_firmware(self):
        install = (ROOT / "scripts/install-ota.sh").read_text().replace("\\\n", "")
        self.assertRegex(install, r"systemctl mask[^\n]*rpi-eeprom-update\.service")

    def test_early_splash_gate_does_not_wait_for_late_cloud_final(self):
        unit = (ROOT / "stage-cloudplay/00-appliance/files/cloudplay-startup.service").read_text()
        dependencies = " ".join(re.findall(r"^(?:Wants|After)=(.*)$", unit, re.MULTILINE)).split()
        self.assertNotIn("cloud-final.service", dependencies)
        self.assertIn("cloudplay-network.service", dependencies)
        greetd = (ROOT / "stage-cloudplay/00-appliance/files/greetd-kiosk.conf").read_text()
        self.assertIn("After=cloud-final.service", greetd)

    def test_full_boot_graph_rejects_cycles_even_when_systemd_exits_zero(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr=(
            "multi-user.target: Found ordering cycle on cloudplay-startup.service/start\n"
            "Job cloudplay-startup.service/start deleted to break ordering cycle\n"))
        with patch.object(verifier.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(AssertionError, "ordering cycle"):
                verifier.verify_boot_order()
        self.assertEqual(run.call_args.args[0][-1], "graphical.target")
        self.assertIn("--generators=yes", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["stdin"], verifier.subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_ota_graph_also_verifies_maintenance_start_transaction(self):
        with patch.object(verifier.Path, "exists", return_value=True), \
                patch.object(verifier.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="", stderr="")) as run:
            verifier.verify_boot_order()
        self.assertEqual(run.call_args.args[0][-2:],
                         ["graphical.target", "cloudplay-maintenance.service"])

    def test_full_boot_graph_accepts_clean_graph_and_rejects_command_failure(self):
        with patch.object(verifier.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="")):
            self.assertFalse(verifier.verify_boot_order()["ordering_cycles"])
        with patch.object(verifier.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="Failed to load unit")):
            with self.assertRaisesRegex(AssertionError, "Failed to load unit"):
                verifier.verify_boot_order()

    def test_ssh_effective_config_parser_and_locked_password_handling(self):
        settings = verifier.ssh_settings(
            "permitrootlogin no\ndenyusers root\ndenyusers cloudplay\npasswordauthentication yes\n")
        self.assertEqual(settings["permitrootlogin"], "no")
        self.assertEqual(set(settings["denyusers"].split()), {"root", "cloudplay"})
        for stored in ("", "!", "*", "!$y$locked"):
            self.assertFalse(verifier.development_password_matches(stored))

    def test_launcher_security(self):
        wrapper = (ROOT / "stage-cloudplay/00-appliance/files/cloudplay-start").read_text()
        self.assertIn("launcher/main.py", wrapper)
        launcher = wrapper + (ROOT / "launcher/host.py").read_text()
        for forbidden in ("--no-sandbox", "--disable-setuid-sandbox", "--remote-debugging",
                          "--force-color-profile", "--ignore-certificate-errors", "sudo ",
                          "--window-size", "--force-device-scale-factor", "--disable-hdr"):
            self.assertNotIn(forbidden, launcher)
        for expected in ("chmod 700", "--load-extension=/opt/gfn-pi-compat",
                         "--kiosk", "--no-first-run", "--ozone-platform=wayland", "cloudplay",
                         "https://play.geforcenow.com/"):
            self.assertIn(expected, launcher)
        self.assertNotIn("desktop-only", launcher)

    def test_greetd_has_no_interactive_greeter(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        config = tomllib.loads((files / "greetd.toml").read_text())
        self.assertEqual(config["terminal"]["vt"], 7)
        for name in ("initial_session", "default_session"):
            self.assertEqual(config[name]["user"], "cloudplay")
            self.assertIn("cloudplay-session", config[name]["command"])
            self.assertNotIn("agreety", config[name]["command"])
        session = (files / "cloudplay-session").read_text()
        self.assertIn("XDG_RUNTIME_DIR", session)
        self.assertIn("dbus-run-session", session)
        self.assertIn("-C /etc/cloudplay/labwc", session)
        self.assertIn("-S /usr/local/bin/cloudplay-browser-session", session)

    def test_no_desktop_ui_or_default_shell_shortcuts(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        self.assertFalse(list(files.glob("*.desktop")))
        self.assertFalse((files / "cloudplay-settings").exists())
        xml = ElementTree.parse(files / "labwc-rc.xml").getroot()
        self.assertTrue(xml.findall("./keyboard/keybind"))
        self.assertTrue(xml.findall("./mouse/context/mousebind"))
        client = xml.find("./mouse/context[@name='Client']")
        self.assertIsNotNone(client)
        self.assertEqual({binding.get("button") for binding in client}, {"Left", "Middle", "Right"})
        for binding in client:
            self.assertEqual([action.get("name") for action in binding], ["Focus", "Raise"])
        self.assertFalse(xml.findall(".//default"))
        execute = xml.findall(".//action[@name='Execute']")
        self.assertEqual(len(execute), 1)
        self.assertEqual(execute[0].get("command"), "/usr/local/bin/cloudplay-home")
        home_binding = xml.find("./keyboard/keybind[@key='C-A-Home']")
        self.assertEqual(home_binding.get("overrideInhibition"), "yes")
        self.assertEqual(home_binding.get("onRelease"), "yes")
        packages = (ROOT / "stage-cloudplay/00-appliance/00-packages-nr").read_text().split()
        self.assertTrue({"greetd", "labwc", "libpam-systemd", "pipewire", "plymouth-themes"} <= set(packages))
        self.assertFalse({"lightdm", "piwiz", "rpd-wayland-core", "zenity"} & set(packages))

    def test_splash_wiring_and_export_verification(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        theme = (files / "cloudplay.plymouth").read_text()
        self.assertIn("ModuleName=script", theme)
        self.assertIn("Cloudplay OS", (files / "cloudplay.script").read_text())
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        for expected in ("plymouth-set-default-theme cloudplay", '"splash"', '"quiet"',
                         '"console=tty3"', "disable_splash=1", "/etc/initramfs-tools/modules"):
            self.assertIn(expected, stage)
        export = (ROOT / "scripts/export-manifest.sh").read_text()
        self.assertLess(export.index("update_initramfs=yes"), export.index("update-initramfs -u"))
        self.assertLess(export.index("update-initramfs"), export.index("verify-kiosk.py"))
        self.assertLess(export.index("verify-kiosk.py"), export.index("package-manifest.py"))
        self.assertIn("runuser -u cloudplay", export)
        self.assertIn("WLR_BACKENDS=headless", export)
        self.assertLess(export.index("mount --bind /dev/shm"), export.index("runuser -u cloudplay"))

    def test_approved_art_is_preserved_and_cleared_footer_has_live_spinner(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        image = (files / "cloudplay.png").read_bytes()
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", image[16:24]), (1920, 1080))
        self.assertEqual(hashlib.sha256(image).hexdigest(),
                         "b2d7338996250ec4f9a34a8534d8bb9d04da7fa5e6305e431633fb184b57ae0b")
        background = (files / "cloudplay-background.png").read_bytes()
        self.assertEqual(hashlib.sha256(background).hexdigest(),
                         "63436f2ffa432dd5461165b47e443c670c60c6968020736d3a4b69ce39257399")
        self.assertEqual(struct.unpack(">II", background[16:24]), (1920, 1080))
        script = (files / "cloudplay.script").read_text()
        self.assertNotIn(b"\r", (files / "cloudplay.script").read_bytes())
        self.assertIn('Image("cloudplay-background.png")', script)
        self.assertNotIn('Image("cloudplay.png")', script)
        self.assertIn('Plymouth.GetMode() == "shutdown"', script)
        self.assertIn('"Shutting down Cloudplay OS"', script)
        self.assertIn('Plymouth.GetMode() == "reboot"', script)
        self.assertIn('"Restarting Cloudplay OS"', script)
        self.assertIn("source_image.Scale", script)
        self.assertIn("Plymouth.SetDisplayMessageFunction(display_message)", script)
        self.assertIn("global.frame++", script)
        frames = [path.read_bytes() for path in sorted(files.glob("spinner-*.png"))]
        self.assertEqual(len(frames), 12)
        self.assertEqual(len({hashlib.sha256(frame).digest() for frame in frames}), 12)
        for frame in frames:
            self.assertEqual(struct.unpack(">II", frame[16:24]), (40, 40))

    def test_network_wait_precedes_plymouth_release_and_compositor_start(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        gate = (files / "cloudplay-startup.service").read_text()
        self.assertIn("Before=plymouth-quit.service plymouth-quit-wait.service greetd.service", gate)
        self.assertIn("TimeoutStartSec=40", gate)
        self.assertIn("RuntimeDirectoryMode=0755", gate)
        greetd = (files / "greetd-kiosk.conf").read_text()
        self.assertIn("After=cloud-final.service cloudplay-startup.service plymouth-quit.service", greetd)
        self.assertIn("Wants=cloud-final.service cloudplay-startup.service plymouth-quit.service", greetd)
        autostart = (files / "labwc-autostart").read_text()
        self.assertIn("/usr/bin/swaybg", autostart)
        self.assertIn("cloudplay-background.png", autostart)
        self.assertNotIn("http://", autostart)

    def test_logging_checks_vendor_dropins_not_just_directory_or_main_file(self):
        main = "[Journal]\n#Storage=auto\n"
        vendor = "[Journal]\nStorage=volatile\n"
        syslog = "[Journal]\nForwardToSyslog=yes\n"
        diagnostics = (ROOT / "stage-cloudplay/00-appliance/files/cloudplay-diagnostics.conf").read_text()
        self.assertEqual(verifier.effective_journal_settings(main + vendor + syslog)["Storage"], "volatile")
        merged = verifier.effective_journal_settings(main + vendor + diagnostics + syslog)
        self.assertEqual(merged["Storage"], "persistent")
        self.assertEqual(merged["SyncIntervalSec"], "15s")
        self.assertEqual(merged["SystemMaxUse"], "64M")
        # A future later override must remain visible instead of reporting the desired setting.
        self.assertEqual(verifier.effective_journal_settings(main + diagnostics + vendor)["Storage"], "volatile")

    def test_user_config_ancestor_must_be_owned_and_private_not_just_leaf(self):
        home = ROOT / "build/mock-home"
        good = SimpleNamespace(st_uid=1000, st_gid=1000, st_mode=0o40700)
        root_owned = SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o40700)
        with patch.object(Path, "lstat", autospec=True, return_value=good):
            metadata = verifier.home_directory_metadata(home, 1000, 1000)
            self.assertEqual(len(metadata), 3)
        def shipped_layout(path):
            return root_owned if path == home / ".config" else good
        with patch.object(Path, "lstat", autospec=True, side_effect=shipped_layout):
            with self.assertRaisesRegex(AssertionError, "config"):
                verifier.home_directory_metadata(home, 1000, 1000)
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn("/home/cloudplay/.config /home/cloudplay/.config/cloudplay", stage)
        export = (ROOT / "scripts/export-manifest.sh").read_text()
        self.assertIn("runuser -u cloudplay -- python3", export)
        self.assertIn(".cloudplay-build-write-check", export)

    def test_network_provisioning_does_not_create_owner_password(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        cloud = (files / "cloud-init-kiosk.cfg").read_text()
        self.assertIn("users: []", cloud)
        self.assertIn("ssh_pwauth: false", cloud)
        network = (files / "network-config").read_text()
        self.assertIn("dhcp4: true", network)
        self.assertNotIn("password:", network)

    def test_crash_backoff_is_bounded_and_recovers_after_stable_run(self):
        failures = 0
        delays = []
        for _ in range(7):
            failures, delay = supervisor.next_retry(failures, 1)
            delays.append(delay)
        self.assertEqual(delays, [2, 4, 8, 16, 32, 300, 2])
        self.assertEqual(supervisor.next_retry(5, 120), (0, 2))

    def test_supervisor_refuses_root(self):
        with patch.object(supervisor.os, "geteuid", return_value=0, create=True):
            with self.assertRaises(SystemExit):
                supervisor.main(["/bin/true"])

    def test_no_release_or_automatic_image_build(self):
        workflow = (ROOT / ".github/workflows/build-image.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("ubuntu-24.04-arm", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("push:", workflow)

    def test_workflow_actions_are_commit_pinned(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            references = re.findall(r"uses:\s+([^\s#]+)", path.read_text())
            for reference in references:
                with self.subTest(workflow=path.name, action=reference):
                    self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")

    def test_preview_promotion_requires_matching_reviewed_image_and_stays_draft(self):
        workflow = (ROOT / ".github/workflows/promote-preview.yml").read_text()
        for required in (
                "workflow_dispatch:", "actions: read", "runs-on: ubuntu-24.04-arm",
                "timeout-minutes: 90",
                '.conclusion == "success" and .headSha == $sha',
                '.isDraft == true and .isPrerelease == true',
                'commits/$RELEASE_TAG', 'provenance/cloudplay-commit.txt',
                "sha256sum --check SHA256SUMS", '"$IMAGE_SHA256"',
                'gh run download "$RUN_ID"', "for attempt in 1 2 3 4 5 6",
                "timeout --kill-after=30 20m", 'gh release upload "$RELEASE_TAG"',
                'state == "starter"',
                "releases/assets/$starter_id", "Existing uploaded asset has the wrong digest",
                '.[0].state == "uploaded" and .[0].digest == $digest'):
            self.assertIn(required, workflow)
        for forbidden in ("--clobber", "gh release create", "gh release edit",
                          "scripts/build-image.sh"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
