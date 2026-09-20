import json
import os
import socket
import struct
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from updater.client import Client, receive_line
from updater.service import Service, parse_request, peer_uid
from updater.state import Config, UpdateError


class ServiceTests(unittest.TestCase):
    def test_accepted_check_is_visible_through_prechecks(self):
        runtime = Mock(config=Config())
        runtime.status.side_effect = lambda: dict(
            phase="available", install_enabled=True, provider_launch_allowed=True,
            available_version="1.1.0", error={"code": "NETWORK"})
        service = Service(runtime)
        service._command = "check"
        service._worker_lock.acquire()
        try:
            status = service.status()
            self.assertEqual(status["phase"], "checking")
            self.assertFalse(status["install_enabled"])
            self.assertTrue(status["provider_launch_allowed"])
            self.assertIsNone(status["available_version"])
            self.assertIsNone(status["error"])
        finally:
            service._worker_lock.release()
        self.assertEqual(service.status()["phase"], "available")
        self.assertEqual(service.status()["error"], {"code": "NETWORK"})

    def test_accepted_install_is_starting_before_runtime_changes_phase(self):
        runtime = Mock(config=Config())
        runtime.status.side_effect = lambda: dict(
            phase="available", install_enabled=True, provider_launch_allowed=True)
        service = Service(runtime)
        service._command = "install"
        service._worker_lock.acquire()
        try:
            status = service.status()
            self.assertEqual(status["phase"], "starting")
            self.assertFalse(status["install_enabled"])
            self.assertFalse(status["provider_launch_allowed"])
        finally:
            service._worker_lock.release()
        self.assertEqual(service.status()["phase"], "available")

    def test_worker_persists_command_for_expected_and_unexpected_errors(self):
        for error in (UpdateError("NETWORK", "Offline"), RuntimeError("Unexpected")):
            with self.subTest(error=error):
                runtime = MagicMock(config=Config())
                runtime.check.side_effect = error
                service = Service(runtime)
                service._worker_lock.acquire()
                with self.assertLogs("cloudplay.updater", level="ERROR"):
                    service._work("check")
                saved = runtime.journal.update.call_args.kwargs["error"]
                self.assertEqual(saved["command"], "check")
                self.assertIn(str(error), saved["message"])
                self.assertFalse(service._worker_lock.locked())

    def test_cleanup_readiness_is_hidden_until_worker_releases_lock(self):
        runtime = Mock(config=Config())
        runtime.status.side_effect = lambda: dict(
            phase="ready_to_restart", can_restart=True, install_enabled=False)
        service = Service(runtime)
        service._command = "install"
        service._worker_lock.acquire()
        try:
            status = service.dispatch("status", 450)
            self.assertEqual(status["phase"], "finishing")
            self.assertFalse(status["can_restart"])
            self.assertFalse(status["channel_change_enabled"])
            self.assertEqual(status["operation"]["name"], "cleanup")
        finally:
            service._worker_lock.release()
        self.assertTrue(service.dispatch("status", 450)["can_restart"])

    def test_install_worker_keeps_lock_through_automatic_restart(self):
        runtime = Mock(config=Config())
        service = Service(runtime)
        calls = []
        runtime.status.side_effect = lambda: dict(phase="ready_to_restart", can_restart=True)
        def install():
            calls.append("install")
            self.assertTrue(service._worker_lock.locked())
            self.assertEqual(service.status()["phase"], "finishing")
        def restart():
            calls.append("restart")
            self.assertTrue(service._worker_lock.locked())
            self.assertEqual(service.status()["phase"], "restarting")
            self.assertFalse(service.status()["can_restart"])
            with self.assertRaisesRegex(UpdateError, "BUSY"):
                service.start("restart")
        runtime.install.side_effect = install
        runtime.restart.side_effect = restart
        service._command = "install"
        service._worker_lock.acquire()
        service._work("install")
        self.assertEqual(calls, ["install", "restart"])
        self.assertFalse(service._worker_lock.locked())

    def test_failed_or_cancelled_install_never_restarts(self):
        for error in (UpdateError("CANCELLED", "cancelled"), OSError("cleanup failed"),
                      UpdateError("SIGNATURE", "untrusted update")):
            with self.subTest(error=error):
                runtime = MagicMock(config=Config())
                runtime.install.side_effect = error
                service = Service(runtime)
                service._worker_lock.acquire()
                with self.assertLogs("cloudplay.updater", level="ERROR"):
                    service._work("install")
                runtime.restart.assert_not_called()
                self.assertEqual(runtime.journal.update.call_args.kwargs["error"]["command"], "install")
                self.assertFalse(service._worker_lock.locked())

    def test_automatic_restart_failure_is_reported_without_retry_loop(self):
        runtime = MagicMock(config=Config())
        runtime.restart.side_effect = UpdateError("SLOT_VERIFY", "readback failed")
        service = Service(runtime)
        service._worker_lock.acquire()
        with self.assertLogs("cloudplay.updater", level="ERROR"):
            service._work("install")
        runtime.install.assert_called_once()
        runtime.restart.assert_called_once()
        self.assertEqual(runtime.journal.update.call_args.kwargs["error"]["command"], "restart")
        self.assertFalse(service._worker_lock.locked())

    def test_only_exact_fixed_commands(self):
        for command in ("status", "check", "install", "cancel", "restart", "enable_beta", "disable_beta"):
            self.assertEqual(parse_request(json.dumps({"command": command})), command)
        for raw in ('{"command":"shell"}', '{"command":"install","url":"https://evil"}',
                    '{"command":"status","command":"install"}', '["status"]',
                    '{"command":[]}', '{"path":"/dev/sda"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(UpdateError):
                    parse_request(raw)

    def test_peer_credentials_use_kernel_uid(self):
        connection = Mock()
        connection.getsockopt.return_value = struct.pack("3i", 123, 456, 789)
        if not hasattr(socket, "SO_PEERCRED"):
            with self.assertRaisesRegex(UpdateError, "IPC_AUTH"):
                peer_uid(connection)
        else:
            self.assertEqual(peer_uid(connection), 456)

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux credential integration")
    def test_real_unix_peer_credentials(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        try:
            self.assertEqual(peer_uid(left), os.getuid())
        finally:
            left.close()
            right.close()

    def test_browser_uid_rejected_even_for_status(self):
        runtime = Mock(config=Config())
        service = Service(runtime)
        with self.assertRaisesRegex(UpdateError, "IPC_AUTH"):
            service.dispatch("status", 1000)
        runtime.status.assert_not_called()

    def test_beta_preference_requires_trusted_identity(self):
        service = Service(Mock(config=Config()))
        for command in ("enable_beta", "disable_beta"):
            with self.assertRaisesRegex(UpdateError, "IPC_AUTH"):
                service.dispatch(command, 1000)
        self.assertIsNone(service._worker)

    def test_status_works_while_hardware_gate_disabled(self):
        runtime = Mock(config=Config())
        runtime.status.return_value = {"phase": "idle", "mutation_enabled": False}
        result = Service(runtime).dispatch("status", 450)
        self.assertFalse(result["mutation_enabled"])

    def test_install_disabled_and_no_worker_started(self):
        runtime = Mock(config=Config())
        service = Service(runtime)
        with self.assertRaisesRegex(UpdateError, "HARDWARE_GATE"):
            service.dispatch("install", 450)
        self.assertIsNone(service._worker)

    def test_worker_creation_failure_does_not_leave_operation_locked(self):
        service = Service(Mock(config=Config()))
        with patch("updater.service.threading.Thread.start", side_effect=RuntimeError("limit")):
            with self.assertRaisesRegex(UpdateError, "WORKER"):
                service.start("check")
        self.assertFalse(service._worker_lock.locked())
        self.assertIsNone(service._worker)

    def test_ipc_size_and_frame_bounds(self):
        connection = Mock()
        connection.recv.return_value = b"x" * 20
        with self.assertRaisesRegex(UpdateError, "IPC_LIMIT"):
            receive_line(connection, 10)
        connection.recv.return_value = b"{}\n{}\n"
        with self.assertRaisesRegex(UpdateError, "IPC"):
            receive_line(connection, 100)

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux socket integration")
    def test_real_socket_fixed_request_response(self):
        runtime = Mock(config=Config(launcher_uid=os.getuid()))
        runtime.status.return_value = {"phase": "idle", "mutation_enabled": False}
        service = Service(runtime)
        left, right = socket.socketpair(socket.AF_UNIX)
        service._clients.acquire()
        thread = threading.Thread(target=service._connection, args=(left,))
        thread.start()
        try:
            right.sendall(b'{"command":"status"}\n')
            response = json.loads(receive_line(right, 65536))
            self.assertTrue(response["ok"])
            self.assertFalse(response["status"]["mutation_enabled"])
        finally:
            right.close()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux socket integration")
    def test_install_restarts_after_request_connection_closes(self):
        runtime = MagicMock(config=Config(launcher_uid=os.getuid()))
        runtime.status.side_effect = lambda: dict(phase="staging")
        finish_install = threading.Event()
        runtime.install.side_effect = lambda: finish_install.wait(5)
        service = Service(runtime)
        left, right = socket.socketpair(socket.AF_UNIX)
        service._clients.acquire()
        connection_thread = threading.Thread(target=service._connection, args=(left,))
        with patch.object(Config, "mutation_gate"):
            connection_thread.start()
            try:
                right.sendall(b'{"command":"install"}\n')
                response = json.loads(receive_line(right, 65536))
                self.assertTrue(response["ok"])
                right.close()
                connection_thread.join(5)
                self.assertFalse(connection_thread.is_alive())
                self.assertTrue(service._worker.is_alive())
                runtime.restart.assert_not_called()
            finally:
                right.close()
                finish_install.set()
                connection_thread.join(5)
                if service._worker is not None:
                    service._worker.join(5)
        runtime.install.assert_called_once()
        runtime.restart.assert_called_once()
        self.assertFalse(service._worker.is_alive())

    def test_client_does_not_accept_arbitrary_command(self):
        with self.assertRaisesRegex(UpdateError, "COMMAND"):
            Client().request("rm -rf /")

    def test_request_deadline_is_total_not_per_byte(self):
        connection = Mock()
        connection.recv.return_value = b"a"
        with patch("updater.client.time.monotonic", side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(UpdateError, "IPC_TIMEOUT"):
                receive_line(connection, 1024, timeout=3)
        connection.recv.assert_called_once()

    def test_rate_limit_mutations(self):
        runtime = Mock(config=Config())
        service = Service(runtime)
        service.dispatch("cancel", 450)
        with self.assertRaisesRegex(UpdateError, "RATE_LIMIT"):
            service.dispatch("cancel", 450)
        runtime.cancel.assert_called_once()

    def test_rejected_command_cannot_disable_candidate_deadline_phase(self):
        runtime = MagicMock(config=Config())
        runtime.check.side_effect = UpdateError("BUSY", "candidate in progress")
        service = Service(runtime)
        service._worker_lock.acquire()
        with self.assertLogs("cloudplay.updater", level="ERROR"):
            service._work("check")
        self.assertEqual(set(runtime.journal.update.call_args.kwargs), {"error"})
        runtime.journal.operation.assert_called_once_with(timeout=5)
        runtime._error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
