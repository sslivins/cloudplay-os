import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))
import host
import gamepad
import main


class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "build/tests" / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.work)
        self.uid = patch.object(host.os, "getuid", return_value=self.work.stat().st_uid, create=True)
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def test_fixed_urls_and_persistent_separate_profiles(self):
        profile = self.work / "chromium-profile"
        profile.mkdir()
        cookies = profile / "Cookies"
        cookies.write_bytes(b"existing-profile-fixture")
        with patch.object(Path, "is_file", return_value=True):
            gfn = host.browser_command("gfn", self.work)
            xbox = host.browser_command("xbox", self.work)
        self.assertEqual(cookies.read_bytes(), b"existing-profile-fixture")
        self.assertEqual(gfn[-1], "https://play.geforcenow.com/")
        self.assertEqual(xbox[-1], "https://www.xbox.com/play")
        self.assertEqual(host.SERVICES["xbox"][0], "Xbox Cloud Gaming")
        self.assertIn("--user-data-dir=" + str(profile), gfn)
        self.assertIn("--user-data-dir=" + str(self.work / "xbox-profile"), xbox)
        self.assertIn("--load-extension=/opt/gfn-pi-compat", gfn)
        self.assertIn("--disable-extensions", xbox)
        for command in (gfn, xbox):
            self.assertIn("--kiosk", command)
            self.assertNotIn("--no-sandbox", command)
            self.assertFalse(any("remote-debugging" in arg for arg in command))
        for invalid in ("https://example.com", "gfn --no-sandbox", "shell", "../gfn"):
            with self.assertRaises(KeyError):
                host.browser_command(invalid, self.work)

    def test_service_specific_recovery_labels(self):
        self.assertEqual(main.action_labels("gfn"), (
            "Return to GeForce NOW", "Reload GeForce NOW", "Cloudplay OS Main Menu"))
        self.assertEqual(main.action_labels("xbox"), (
            "Return to Xbox Cloud Gaming", "Reload Xbox Cloud Gaming",
            "Cloudplay OS Main Menu"))

    @unittest.skipIf(os.name == "nt", "Unix profile permissions")
    def test_private_profile_rejects_symlink_and_preserves_target(self):
        target = self.work / "target"
        target.mkdir()
        mode = stat.S_IMODE(target.stat().st_mode)
        (self.work / "xbox-profile").symlink_to(target, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            host.browser_command("xbox", self.work)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)

    def test_missing_gfn_extension_does_not_block_xbox(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(RuntimeError):
                host.browser_command("gfn", self.work)
            self.assertEqual(host.browser_command("xbox", self.work)[-1],
                             "https://www.xbox.com/play")

    def browser(self):
        with patch.object(host.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="inactive\n")) as run:
            browser = host.Browser(self.work)
        self.assertIn("stop", run.call_args_list[0].args[0])
        return browser

    def test_start_uses_fixed_user_cgroup_with_complete_termination(self):
        browser = self.browser()
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-test",
                                    "XDG_RUNTIME_DIR": str(self.work)}), patch.object(
                browser, "stop") as stop, patch.object(host.subprocess, "run") as run:
            browser.start("xbox")
        stop.assert_called_once()
        command = run.call_args.args[0]
        for arg in ("--user", "--unit=cloudplay-stream.service", "--collect",
                    "--service-type=exec", "--property=ExitType=cgroup",
                    "--property=KillMode=control-group", "--property=TimeoutStopSec=2s",
                    "--property=SendSIGKILL=yes", "--property=Restart=no"):
            self.assertIn(arg, command)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(browser.service, "xbox")

    def test_home_waits_for_confirmed_inactive_cgroup_and_keeps_profiles(self):
        browser = self.browser()
        browser.service = "gfn"
        marker = self.work / "Cookies"
        marker.write_text("fixture")
        with patch.object(host.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="inactive\n")) as run:
            browser.stop()
        self.assertEqual(run.call_args_list[0].args[0],
                         ["/usr/bin/systemctl", "--user", "stop", browser.UNIT])
        self.assertIsNone(browser.service)
        self.assertTrue(marker.exists())

    def test_stop_failure_does_not_claim_home_or_clear_service(self):
        browser = self.browser()
        browser.service = "gfn"
        for result in (SimpleNamespace(returncode=0, stdout="active\n"),
                       SimpleNamespace(returncode=1, stdout=""),
                       SimpleNamespace(returncode=0, stdout="unexpected")):
            with self.subTest(result=result), patch.object(host.subprocess, "run",
                                                          return_value=result):
                with self.assertRaises(RuntimeError):
                    browser.stop()
                self.assertEqual(browser.service, "gfn")
        with patch.object(host.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("systemctl", 3)):
            with self.assertRaises(subprocess.TimeoutExpired):
                browser.stop()

    def test_exited_service_returns_home_but_home_does_not_poll_systemd(self):
        browser = self.browser()
        with patch.object(host.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="0\n")) as run:
            self.assertFalse(browser.exited())
            run.assert_not_called()
            browser.service = "xbox"
            self.assertTrue(browser.exited())
            self.assertIn("--property=MainPID", run.call_args.args[0])


@unittest.skipUnless(sys.platform == "linux", "Linux Unix credential socket")
class ControlTest(unittest.TestCase):
    def setUp(self):
        # Use the native temporary filesystem: WSL Windows mounts cannot bind
        # SOCK_SEQPACKET, and AF_UNIX paths must also fit its 108-byte limit.
        temporary = tempfile.TemporaryDirectory(prefix="cloudplay-control-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.control = host.Control(self.work)
        self.addCleanup(self.control.close)

    def send(self, data):
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        peer.connect(str(self.control.path))
        peer.sendall(data)
        self.addCleanup(peer.close)

    def test_only_home_packet_from_owner_is_accepted(self):
        for bad in (b"reload", b"xbox", b"home\n", b"http://example.com", b"home" + b"x" * 512):
            self.send(bad)
            self.assertFalse(self.control.poll())
        self.send(b"home")
        self.assertTrue(self.control.poll())
        self.assertFalse(self.control.poll())
        self.assertEqual(stat.S_IMODE(self.control.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.control.path.parent.stat().st_mode), 0o700)

    def test_foreign_uid_rejected_even_if_socket_permissions_were_bypassed(self):
        self.send(b"home")
        with patch.object(host.os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(self.control.poll())

    def test_idle_client_is_bounded_and_cannot_block_later_home(self):
        idle = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        idle.connect(str(self.control.path))
        self.addCleanup(idle.close)
        self.send(b"home")
        self.assertTrue(self.control.poll())

    def test_second_launcher_cannot_replace_live_control(self):
        with self.assertRaisesRegex(RuntimeError, "already running"):
            host.Control(self.work)
        self.send(b"home")
        self.assertTrue(self.control.poll())

    def test_stale_socket_recovered(self):
        self.control.socket.close()
        self.control = host.Control(self.work)
        self.addCleanup(self.control.close)
        self.send(b"home")
        self.assertTrue(self.control.poll())


class PadTest(unittest.TestCase):
    def test_deliberate_two_button_hold_fires_once_until_release(self):
        pad = gamepad.Pad()
        pad.event(1, gamepad.SELECT, 1)
        self.assertFalse(pad.held(0))
        pad.event(1, gamepad.START, 1)
        self.assertFalse(pad.held(1))
        self.assertFalse(pad.held(2.99))
        self.assertTrue(pad.held(3))
        self.assertFalse(pad.held(40))
        pad.event(1, gamepad.SELECT, 0)
        pad.event(1, gamepad.SELECT, 1)
        self.assertFalse(pad.held(50))
        self.assertTrue(pad.held(52))

    def test_short_hold_release_and_event_loss_never_trigger(self):
        pad = gamepad.Pad()
        for code in (gamepad.SELECT, gamepad.START):
            pad.event(1, code, 1)
        self.assertFalse(pad.held(0))
        pad.event(1, gamepad.START, 0)
        self.assertFalse(pad.held(3))
        pad.event(1, gamepad.START, 1)
        self.assertFalse(pad.held(4))
        pad.event(0, 3, 0)
        self.assertFalse(pad.held(9))
        self.assertTrue(pad.sync_lost)

    def test_dpad_and_button_edges_only_no_repeat_or_initial_stick_noise(self):
        pad = gamepad.Pad()
        self.assertEqual(pad.event(1, gamepad.A, 1), "accept")
        self.assertIsNone(pad.event(1, gamepad.A, 2))
        self.assertIsNone(pad.event(1, gamepad.A, 1))
        self.assertEqual(pad.event(3, 16, -1), "left")
        self.assertIsNone(pad.event(3, 16, -1))
        self.assertIsNone(pad.event(3, 0, 32767))
        self.assertEqual(pad.event(1, 545, 1), "down")

    def test_missing_gamepads_do_not_break_menu(self):
        reader = gamepad.Gamepads(Path("/nonexistent-cloudplay-gamepads"))
        self.assertEqual(reader.poll(True), [])
        reader.close()

    @unittest.skipUnless(sys.platform == "linux", "Linux evdev ioctl")
    def test_capability_filter_excludes_keyboards_and_non_gamepads(self):
        import fcntl
        def response(codes):
            def fill(fd, request, bits):
                self.assertEqual(request & 255, 0x21)
                for code in codes:
                    bits[code // 8] |= 1 << (code % 8)
            return fill
        pad = (gamepad.A, gamepad.SELECT, gamepad.START)
        for codes, expected in ((pad, True), ((*pad, 30), False),
                                ((gamepad.A,), False), ((30, 31), False)):
            with patch.object(fcntl, "ioctl", side_effect=response(codes)):
                self.assertEqual(gamepad.allowed(42), expected)

    def test_permission_denied_is_optional_and_device_count_is_bounded(self):
        paths = [Path(f"/dev/input/cloudplay-gamepad-event{i}") for i in range(20)]
        directory = Mock()
        directory.glob.return_value = paths
        reader = gamepad.Gamepads(directory)
        with self.assertLogs("gamepad", level="WARNING") as logs, patch.object(
                gamepad.os, "open", side_effect=PermissionError) as opened, patch.object(
                gamepad.os, "O_NONBLOCK", 0, create=True), patch.object(
                gamepad.os, "O_CLOEXEC", 0, create=True):
            self.assertEqual(reader.poll(True), [])
            reader.next_scan = 0
            self.assertEqual(reader.poll(True), [])
        self.assertEqual(opened.call_count, 8)
        self.assertEqual(len(logs.output), 4)
        reader.close()

    def test_no_menu_input_forwarded_during_stream_and_unplug_closes_only_owned_fd(self):
        directory = Mock()
        reader = gamepad.Gamepads(directory)
        reader.next_scan = float("inf")
        path = Path("selected")
        reader.devices[path] = (123, gamepad.Pad())
        event = gamepad.EVENT.pack(0, 0, 1, gamepad.A, 1)
        with patch.object(gamepad.os, "read", return_value=event):
            self.assertEqual(reader.poll(False), [])
        with patch.object(gamepad.os, "read", side_effect=OSError), patch.object(
                gamepad.os, "close") as close:
            self.assertEqual(reader.poll(True), [])
        close.assert_called_once_with(123)
        self.assertFalse(reader.devices)

    def test_held_menu_buttons_must_return_neutral_when_dialog_opens(self):
        reader = gamepad.Gamepads(Mock())
        reader.next_scan = float("inf")
        pad = gamepad.Pad()
        reader.devices[Path("selected")] = (123, pad)
        down = gamepad.EVENT.pack(0, 0, 1, gamepad.A, 1)
        up = gamepad.EVENT.pack(0, 0, 1, gamepad.A, 0)
        with patch.object(gamepad.os, "read", return_value=down):
            self.assertEqual(reader.poll(True), [])
        with patch.object(gamepad.os, "read", return_value=up):
            self.assertEqual(reader.poll(True), [])
        with patch.object(gamepad.os, "read", return_value=down):
            self.assertEqual(reader.poll(True), ["accept"])

    def test_saturated_event_queue_cannot_infer_a_hold_from_stale_state(self):
        reader = gamepad.Gamepads(Mock())
        reader.next_scan = float("inf")
        pad = gamepad.Pad()
        pad.buttons = {gamepad.SELECT, gamepad.START}
        pad.since = 0
        reader.devices[Path("selected")] = (123, pad)
        data = gamepad.EVENT.pack(0, 0, 0, 0, 0) * 64
        with patch.object(gamepad.os, "read", return_value=data):
            self.assertEqual(reader.poll(False), [])
        self.assertIsNone(pad.since)


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("CLOUDPLAY_SYSTEMD_TEST") == "1",
                     "Opt-in real Linux user-manager cgroup test")
class UserCgroupTest(unittest.TestCase):
    def test_descendants_and_orphans_stop_even_after_leader_exits(self):
        work = Path.cwd() / "build" / ("cgroup-" + uuid.uuid4().hex[:8])
        work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, work)
        test_browser = type("TestBrowser", (host.Browser,), {
            "UNIT": "cloudplay-launcher-test-" + uuid.uuid4().hex + ".service"})
        browser = test_browser(work)
        self.addCleanup(browser.stop)
        marker = work / "descendant"
        child_code = (
            "import signal,time,os; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        leader_code = (
            "import subprocess; "
            f"subprocess.Popen(['/usr/bin/python3', '-c', {child_code!r}], start_new_session=True)"
        )
        with patch.object(host, "browser_command",
                          return_value=["/usr/bin/python3", "-c", leader_code]), patch.dict(
                os.environ, {"WAYLAND_DISPLAY": "wayland-test"}):
            browser.start("xbox")
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        self.assertTrue(browser.active(), "Leader exited, but its child must remain tracked")
        deadline = time.monotonic() + 2
        while not browser.exited() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(browser.exited(), "Home must detect a dead leader despite remaining helpers")
        # A new Home instance recovers the fixed owned unit after the old one died.
        recovered = test_browser(work)
        self.addCleanup(recovered.stop)
        self.assertFalse(recovered.active())
        child_stat = Path("/proc") / marker.read_text() / "stat"
        if child_stat.exists():
            self.assertEqual(child_stat.read_text().split()[2], "Z")


class LauncherWiringTest(unittest.TestCase):
    def test_every_runtime_file_is_staged_installed_and_manifested(self):
        build = (ROOT / "scripts/build-image.sh").read_text()
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        verifier = (ROOT / "scripts/verify-kiosk.py").read_text()
        self.assertIn("launcher/{main.py,host.py,gamepad.py,updates.py,maintenance.py,heartbeat.py}", build)
        self.assertIn("cp -r launcher/assets", build)
        self.assertIn('cp -a "${inputs}/launcher"', stage)
        for file in ("main.py", "host.py", "gamepad.py", "updates.py", "maintenance.py", "heartbeat.py"):
            self.assertIn('"' + file + '"', verifier)
        self.assertIn("cloudplay-home", stage)
        self.assertIn("71-cloudplay-gamepad.rules", stage)
        self.assertIn('"input"', verifier)
        self.assertIn("cloudplay-gamepad", verifier)
        for name in ("cloudplay-logo.png", "geforce-now-logo.png",
                     "xbox-cloud-gaming-logo.png", "keyboard-icon.png",
                     "controller-icon.png"):
            self.assertIn('"' + name + '"', verifier)
            asset = ROOT / "launcher/assets" / name
            self.assertTrue(asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        export = (ROOT / "scripts/export-manifest.sh").read_text()
        self.assertIn("check-launcher.py", export)
        self.assertIn("test -f /run/cloudplay-config-check/home-smoke.json", export)

    def test_root_network_helper_has_no_new_process_control(self):
        source = (ROOT / "onboarding/service.py").read_text()
        for forbidden in ("cloudplay-stream", "systemd-run", "launcher/", "request_home"):
            self.assertNotIn(forbidden, source)
        source = (ROOT / "launcher/host.py").read_text()
        self.assertNotIn("AF_INET", source)
        self.assertNotIn("shell=True", source)
        self.assertIn("SO_PEERCRED", source)


if __name__ == "__main__":
    unittest.main()
