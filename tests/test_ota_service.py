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
    def test_only_exact_fixed_commands(self):
        for command in ("status", "check", "install", "cancel", "restart"):
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
            service.dispatch("status", 1001)
        runtime.status.assert_not_called()

    def test_status_works_while_hardware_gate_disabled(self):
        runtime = Mock(config=Config())
        runtime.status.return_value = {"phase": "idle", "mutation_enabled": False}
        result = Service(runtime).dispatch("status", 1000)
        self.assertFalse(result["mutation_enabled"])

    def test_install_disabled_and_no_worker_started(self):
        runtime = Mock(config=Config())
        service = Service(runtime)
        with self.assertRaisesRegex(UpdateError, "HARDWARE_GATE"):
            service.dispatch("install", 1000)
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
        runtime = Mock(config=Config())
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
        service.dispatch("cancel", 1000)
        with self.assertRaisesRegex(UpdateError, "RATE_LIMIT"):
            service.dispatch("cancel", 1000)
        runtime.cancel.assert_called_once()

    def test_rejected_command_cannot_disable_candidate_deadline_phase(self):
        runtime = MagicMock(config=Config())
        runtime.check.side_effect = UpdateError("BUSY", "candidate in progress")
        service = Service(runtime)
        service._worker_lock.acquire()
        with self.assertLogs("cloudplay.updater", level="ERROR"):
            service._work("check")
        self.assertEqual(set(runtime.journal.update.call_args.kwargs), {"error"})
        runtime._error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
