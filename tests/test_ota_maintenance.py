from contextlib import nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from updater import maintenance as m
from updater.service import Service
from updater.state import Config, UpdateError

CONFIG = replace(Config(), launcher_uid=450, browser_uid=1000, socket_gid=450)


class MaintenanceTests(unittest.TestCase):
    def test_runtime_directory_is_pinned_after_pam_environment(self):
        unit = (Path(__file__).resolve().parents[1]
                / "image-build/cloudplay-maintenance.service").read_text()
        self.assertIn("PAMName=login", unit)
        self.assertIn("ExecStart=/usr/bin/env XDG_RUNTIME_DIR=/run/cloudplay-update-ui ", unit)

    def test_browser_can_only_request_trusted_screen_not_actions_or_exit(self):
        m.authorize(1000, "open", CONFIG)
        m.authorize(450, "open", CONFIG)
        m.authorize(450, "close", CONFIG)
        for uid, command in ((1000, "close"), (1000, "install"), (450, "install"),
                             (0, "shell"), (999, "open"), (0, "restart")):
            with self.subTest(uid=uid, command=command), self.assertRaises(UpdateError):
                m.authorize(uid, command, CONFIG)

    def fixture(self, directory, remaining=False):
        runtime = Mock(config=CONFIG)
        runtime.journal.operation.side_effect = nullcontext
        runtime.status.return_value = {"provider_launch_allowed": True}
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == "pgrep":
                return SimpleNamespace(returncode=0 if remaining else 1, stdout="", stderr="")
            if args == ["systemctl", "is-active", "greetd.service"]:
                return SimpleNamespace(returncode=0, stdout="active", stderr="")
            return SimpleNamespace(returncode=0, stdout="inactive", stderr="")
        return m.Portal(runtime, runner=run, marker=Path(directory) / "active"), calls

    def test_gaming_identity_is_terminated_before_trusted_compositor_starts(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "provider_lease", side_effect=lambda _: nullcontext()):
            portal, calls = self.fixture(directory)
            self.assertEqual(portal.transition(1000, "open"), {"session": "updates"})
            self.assertLess(calls.index(["systemctl", "stop", "greetd.service"]),
                            calls.index(["loginctl", "terminate-user", "1000"]))
            self.assertLess(calls.index(["pgrep", "-u", "1000"]),
                            calls.index(["systemctl", "start", m.UNIT]))
            self.assertTrue(portal.marker.exists())
            portal.runtime.status.assert_not_called()

    def test_remaining_browser_processes_block_trusted_ui(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "provider_lease", side_effect=lambda _: nullcontext()):
            portal, calls = self.fixture(directory, remaining=True)
            with self.assertLogs(m.LOG, level="ERROR"), self.assertRaisesRegex(UpdateError, "SESSION"):
                portal.transition(1000, "open")
            self.assertNotIn(["systemctl", "start", m.UNIT], calls)
            self.assertNotIn(["systemctl", "start", "greetd.service"], calls)
            self.assertIn(["systemctl", "--no-block", "start", "cloudplay-update-recovery.service"], calls)

    def test_no_return_to_gaming_during_candidate_or_partition_writes(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "provider_lease", side_effect=lambda _: nullcontext()):
            portal, calls = self.fixture(directory)
            portal.runtime.status.return_value = {"provider_launch_allowed": False}
            with self.assertLogs(m.LOG, level="ERROR"), self.assertRaisesRegex(UpdateError, "BUSY"):
                portal.transition(450, "close")
            self.assertEqual(calls, [])

    def test_remaining_pid_must_still_belong_to_gaming_identity(self):
        with patch.object(m.os, "pidfd_open", return_value=42, create=True), \
                patch.object(m.signal, "pidfd_send_signal", create=True) as send, \
                patch.object(m.os, "close") as close, \
                patch.object(m.Path, "read_text", return_value="Uid:\t0\t0\t0\t0\n"):
            with self.assertRaisesRegex(UpdateError, "identity changed"):
                m.kill_session_processes(1000, "123")
            send.assert_not_called()
            close.assert_called_once_with(42)

    def test_verified_leftover_uses_pidfd_not_reusable_pid(self):
        with patch.object(m.os, "pidfd_open", return_value=42, create=True) as opened, \
                patch.object(m.signal, "pidfd_send_signal", create=True) as send, \
                patch.object(m.signal, "SIGKILL", 9, create=True), \
                patch.object(m.os, "close") as close, \
                patch.object(m.Path, "read_text", return_value="Uid:\t1000\t1000\t1000\t1000\n"), \
                self.assertLogs(m.LOG, level="WARNING"):
            m.kill_session_processes(1000, "123")
            opened.assert_called_once_with(123)
            send.assert_called_once_with(42, m.signal.SIGKILL)
            close.assert_called_once_with(42)

    def test_trusted_exit_stops_private_compositor_before_restoring_gaming(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "provider_lease", side_effect=lambda _: nullcontext()):
            portal, calls = self.fixture(directory)
            portal.marker.write_text("active")
            portal.transition(450, "close")
            self.assertEqual(calls, [["systemctl", "--no-block", "stop", m.UNIT],
                                     ["loginctl", "terminate-user", "450"],
                                     ["systemctl", "--no-block", "stop", "user-450.slice"],
                                     ["pgrep", "-u", "450"],
                                     ["systemctl", "stop", m.UNIT],
                                     ["systemctl", "start", "greetd.service"],
                                     ["systemctl", "is-active", "greetd.service"]])
            self.assertFalse(portal.marker.exists())

    def test_stale_trusted_process_blocks_return_to_gaming(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "provider_lease", side_effect=lambda _: nullcontext()):
            portal, calls = self.fixture(directory, remaining=True)
            portal.marker.write_text("active")
            with self.assertLogs(m.LOG, level="ERROR"), self.assertRaisesRegex(UpdateError, "SESSION"):
                portal.transition(450, "close")
            self.assertNotIn(["systemctl", "start", "greetd.service"], calls)
            self.assertTrue(portal.marker.exists())


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "requires Linux root for isolated child UID changes")
class KernelBoundaryTests(unittest.TestCase):
    @staticmethod
    def child(uid, path, command="status"):
        code = (
            "import socket,json,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(4); "
            "s.connect(sys.argv[1]); s.sendall(json.dumps({'command':sys.argv[2]}).encode()+b'\\n'); "
            "print(s.recv(65536).decode()); s.close()"
        )
        def identity():
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        return subprocess.run([sys.executable, "-c", code, str(path), command],
                              preexec_fn=identity, capture_output=True, text=True, timeout=8)

    def test_real_kernel_credentials_deny_browser_and_allow_trusted_identity(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-peers-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            path = root / "updater.sock"
            runtime = Mock(config=CONFIG)
            runtime.status.return_value = {"phase": "idle", "mutation_enabled": False}
            service = Service(runtime)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
                path.chmod(0o666)  # Deliberately bypass DAC to test SO_PEERCRED too.
                listener.listen(2)
                listener.settimeout(6)
                def accept():
                    for _ in range(2):
                        connection, _ = listener.accept()
                        service._clients.acquire()
                        service._connection(connection)
                thread = threading.Thread(target=accept)
                thread.start()
                browser = self.child(1000, path, "install")
                trusted = self.child(450, path)
                thread.join(8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(browser.returncode, 0, browser.stderr)
            self.assertEqual(trusted.returncode, 0, trusted.stderr)
            self.assertEqual(json.loads(browser.stdout)["error"]["code"], "IPC_AUTH")
            self.assertTrue(json.loads(trusted.stdout)["ok"])
            runtime.install.assert_not_called()
            runtime.status.assert_called_once()

    def test_broker_clients_can_traverse_parent_under_private_service_umask(self):
        code = """
import os, sys
from pathlib import Path
from updater import maintenance as m
from updater.state import Config
config = Config()
m.SOCKET = Path(sys.argv[1])
class Portal:
    def __init__(self, runtime):
        pass
    def transition(self, uid, command):
        m.authorize(uid, command, config)
        return {"session": "authorized"}
m.Portal = Portal
os.umask(0o077)
m.serve(config)
"""
        for existing in (False, True):
            with self.subTest(bootstrap_created=existing), \
                    tempfile.TemporaryDirectory(prefix="cloudplay-broker-", dir="/run") as directory:
                root = Path(directory)
                root.chmod(0o755)
                path = root / "maintenance" / "control.sock"
                if existing:
                    path.parent.mkdir(mode=0o700)
                process = subprocess.Popen(
                    [sys.executable, "-c", code, str(path)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while not path.is_socket():
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Broker did not publish its socket")
                        time.sleep(0.02)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
                    self.assertEqual((path.parent / "broker").stat().st_mode & 0o777, 0o700)
                    for uid, command, accepted in (
                        (450, "close", True), (1000, "open", True), (1000, "close", False),
                    ):
                        result = self.child(uid, path, command)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIs(json.loads(result.stdout)["ok"], accepted)
                finally:
                    process.terminate()
                    process.communicate(timeout=8)

    def test_browser_cannot_connect_to_trusted_compositor_namespace(self):
        with tempfile.TemporaryDirectory(prefix="cloudplay-seat-") as directory:
            root = Path(directory)
            os.chown(root, 450, 450)
            root.chmod(0o700)
            path = root / "wayland-0"
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
                path.chmod(0o777)
                listener.listen(1)
                result = self.child(1000, path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PermissionError", result.stderr)


if __name__ == "__main__":
    unittest.main()
