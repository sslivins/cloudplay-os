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
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from updater import maintenance as m
from updater.service import Service
from updater.state import Config, UpdateError

CONFIG = replace(Config(), launcher_uid=450, browser_uid=1000, socket_gid=450)


class MaintenanceTests(unittest.TestCase):
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

    def test_trusted_exit_stops_private_compositor_before_restoring_gaming(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "provider_lease", side_effect=lambda _: nullcontext()):
            portal, calls = self.fixture(directory)
            portal.marker.write_text("active")
            portal.transition(450, "close")
            self.assertEqual(calls, [["systemctl", "stop", m.UNIT],
                                     ["systemctl", "start", "greetd.service"]])
            self.assertFalse(portal.marker.exists())


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
