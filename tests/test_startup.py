import contextlib
import io
import json
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import sys
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "onboarding"))
import client
import network
import service
import readiness


class ManagerError(Exception):
    def __init__(self, name="org.freedesktop.NetworkManager.AlreadyEnabledOrDisabled"):
        self.name = name

    def get_dbus_name(self):
        return self.name


class ManagerProtocol:
    """Match NM 1.52.1 impl_manager_enable, including its non-idempotent error."""
    def __init__(self, enabled=True, wireless=True):
        self.enabled, self.wireless = enabled, wireless
        self.calls = []

    def call(self, path, interface, method, *args):
        self.calls.append((method, args))
        if method == "GetAll":
            return {"NetworkingEnabled": self.enabled, "WirelessEnabled": self.wireless}
        if method == "Enable":
            if self.enabled == args[0]:
                raise ManagerError()
            self.enabled = args[0]
        elif method == "Set":
            self.wireless = bool(args[-1])

    def adapter(self):
        net = network.Network.__new__(network.Network)
        net.dbus = SimpleNamespace(Boolean=bool, DBusException=ManagerError)
        net.call = Mock(side_effect=self.call)
        net.ap_connection = None
        return net


class ManagerStartupTest(unittest.TestCase):
    def test_redundant_enable_is_not_sent(self):
        protocol = ManagerProtocol()
        net = protocol.adapter()
        net.enable()
        self.assertEqual([name for name, _ in protocol.calls], ["GetAll"])

    def test_only_disabled_networking_and_radio_are_enabled(self):
        protocol = ManagerProtocol(enabled=False, wireless=False)
        protocol.adapter().enable()
        self.assertTrue(protocol.enabled)
        self.assertTrue(protocol.wireless)
        self.assertEqual([name for name, _ in protocol.calls], ["GetAll", "Enable", "Set"])

    def test_concurrent_enable_race_is_accepted_only_when_now_enabled(self):
        protocol = ManagerProtocol(enabled=False)
        net = protocol.adapter()
        original = protocol.call
        def racing(path, interface, method, *args):
            if method == "Enable":
                protocol.enabled = True
            return original(path, interface, method, *args)
        net.call.side_effect = racing
        net.enable()
        self.assertEqual([name for name, _ in protocol.calls], ["GetAll", "Enable", "GetAll"])

    def test_real_permission_failure_is_not_suppressed(self):
        protocol = ManagerProtocol(enabled=False)
        net = protocol.adapter()
        original = protocol.call
        def denied(path, interface, method, *args):
            if method == "Enable":
                raise ManagerError("org.freedesktop.NetworkManager.PermissionDenied")
            return original(path, interface, method, *args)
        net.call.side_effect = denied
        with self.assertRaises(ManagerError):
            net.enable()

    def test_diagnostics_log_dbus_name_not_exception_message_or_credentials(self):
        error = ManagerError()
        error.args = ("do not log a Wi-Fi password",)
        self.assertEqual(service.failure_code(error),
                         "ManagerError:org.freedesktop.NetworkManager.AlreadyEnabledOrDisabled")
        self.assertEqual(service.failure_code(ValueError("private credentials")), "ValueError")
        self.assertEqual(service.failure_code(ManagerError("invalid name containing secrets")), "ManagerError")

    def test_dbus_call_retains_error_identity_and_logs_operation_not_arguments(self):
        error = ManagerError("org.freedesktop.NetworkManager.PermissionDenied")
        error.args = ("private password contents",)
        proxy = SimpleNamespace(AddAndActivateConnection2=Mock(side_effect=error))
        net = network.Network.__new__(network.Network)
        net.bus = SimpleNamespace(get_object=Mock(return_value=proxy))
        net.dbus = SimpleNamespace(Interface=lambda obj, _: obj, DBusException=ManagerError)
        with self.assertRaises(ManagerError) as caught:
            net.call(network.ROOT, network.NM, "AddAndActivateConnection2",
                     {"password": "never-log-this-secret"})
        self.assertIs(caught.exception, error)
        code = service.failure_code(error)
        self.assertIn("@org.freedesktop.NetworkManager.AddAndActivateConnection2", code)
        self.assertIn("PermissionDenied", code)
        self.assertNotIn("private", code)
        self.assertNotIn("never-log", code)

    def test_system_bus_connection_error_has_safe_stage_context(self):
        error = ManagerError("org.freedesktop.DBus.Error.FileNotFound")
        fake = SimpleNamespace(SystemBus=Mock(side_effect=error), DBusException=ManagerError)
        with patch.dict(sys.modules, {"dbus": fake}), self.assertRaises(ManagerError):
            network.Network()
        self.assertEqual(service.failure_code(error),
                         "ManagerError:org.freedesktop.DBus.Error.FileNotFound@SystemBus.connect")

    def test_untrusted_operation_text_is_not_logged(self):
        error = ManagerError()
        error.cloudplay_operation = "SSID or password\nsecret"
        self.assertNotIn("@", service.failure_code(error))

    def test_connected_ethernet_does_not_call_radio_initialization(self):
        work = ROOT / "build/tests" / str(uuid.uuid4())
        work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, work)
        net = ManagerProtocol().adapter()
        net.snapshot = Mock(return_value={"connected": True, "has_wifi": True})
        net.scan = Mock()
        controller = service.Controller(net, work / "state", work / "dns", {"US": "United States"})
        def done(**_):
            controller.stop.set()
            raise service.queue.Empty
        controller.commands.get = Mock(side_effect=done)
        # Bound even the old failure path instead of waiting five real seconds.
        with patch.object(controller.stop, "wait", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            controller.run()
        self.assertFalse(controller.failed)
        self.assertTrue(controller.status["connected"])
        self.assertEqual(controller.status["phase"], "ready")
        net.call.assert_not_called()
        net.scan.assert_not_called()

    def test_persistent_diagnostics_are_bounded_without_enabling_ssh(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        config = (files / "cloudplay-diagnostics.conf").read_text()
        for setting in ("Storage=persistent", "SystemMaxUse=64M", "SystemKeepFree=128M",
                        "RuntimeMaxUse=16M", "SyncIntervalSec=15s", "MaxRetentionSec=7d",
                        "ForwardToConsole=no"):
            self.assertIn(setting, config)
        unit = (files / "cloudplay-network.service").read_text()
        for setting in ("StandardOutput=journal", "StandardError=journal",
                        "SyslogIdentifier=cloudplay-network"):
            self.assertIn(setting, unit)
        stage = (ROOT / "stage-cloudplay/00-appliance/01-run.sh").read_text()
        self.assertIn("install -d -m 2755 -o root -g systemd-journal /var/log/journal", stage)
        self.assertIn("systemctl mask ssh.service ssh.socket", stage)


class BrowserHandoffTest(unittest.TestCase):
    def setUp(self):
        self.runtime = ROOT / "build/tests" / str(uuid.uuid4())
        self.runtime.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.runtime)

    def run_client(self, readiness_values, exited=False, mode="setup"):
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = False
        child = Mock(pid=123456, returncode=1 if exited else None)
        child.poll.return_value = 1 if exited else None
        real_stat = Path.stat
        runtime = self.runtime
        def stat(path, *args, **kwargs):
            if path == runtime:
                return SimpleNamespace(st_uid=1000)
            return real_stat(path, *args, **kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"pwd": SimpleNamespace(
                getpwuid=lambda _: SimpleNamespace(pw_name="cloudplay"))}))
            stack.enter_context(patch.dict(client.os.environ, {
                "XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-test"}))
            stack.enter_context(patch.object(client.os, "getuid", return_value=1000, create=True))
            stack.enter_context(patch.object(client.os, "geteuid", return_value=1000, create=True))
            stack.enter_context(patch.object(Path, "stat", stat))
            stack.enter_context(patch.object(client.threading, "Event", return_value=event))
            stack.enter_context(patch.object(client.signal, "signal"))
            stack.enter_context(patch.object(client.signal, "SIGHUP", 1, create=True))
            stack.enter_context(patch.object(client.signal, "SIGKILL", 9, create=True))
            stack.enter_context(patch.object(client.time, "sleep"))
            stack.enter_context(patch.object(client, "ready", side_effect=readiness_values))
            stack.enter_context(patch.object(client.readiness, "wait_for_mode", return_value=mode))
            popen = stack.enter_context(patch.object(client.subprocess, "Popen", return_value=child))
            kill = stack.enter_context(patch.object(client.os, "killpg", create=True))
            execute = stack.enter_context(patch.object(client.os, "execv"))
            error = None
            try:
                client.main()
            except SystemExit as result:
                error = result.code
        return popen, kill, execute, error

    def test_ethernet_fast_path_executes_gfn_without_localhost_browser(self):
        popen, kill, execute, error = self.run_client([], mode="online")
        self.assertIsNone(error)
        popen.assert_not_called()
        kill.assert_not_called()
        execute.assert_called_once_with("/usr/local/bin/cloudplay-start", ["cloudplay-start"])

    def test_setup_ready_terminates_only_owned_browser_then_executes_gfn(self):
        popen, kill, execute, error = self.run_client([True])
        self.assertIsNone(error)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.args[0][-1], "http://127.0.0.1:8765/")
        self.assertEqual({call.args[0] for call in kill.call_args_list}, {123456})
        self.assertFalse((self.runtime / "cloudplay-network-profile").exists())
        execute.assert_called_once_with("/usr/local/bin/cloudplay-start", ["cloudplay-start"])

    def test_setup_browser_exit_returns_failure_to_outer_backoff_not_false_success(self):
        popen, kill, execute, error = self.run_client([False], exited=True)
        self.assertEqual(error, 1)
        execute.assert_not_called()
        self.assertFalse((self.runtime / "cloudplay-network-profile").exists())

    def test_truncated_real_http_response_is_retryable_not_client_termination(self):
        class Truncated(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "5117")
                self.end_headers()
                self.close_connection = True
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Truncated)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(client, "URL", f"http://127.0.0.1:{server.server_port}"), patch.object(
                    readiness, "network_snapshot", return_value=None):
                self.assertFalse(client.ready())
                self.assertFalse(client.ready())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(issubclass(http.client.IncompleteRead, OSError))
        self.assertFalse(issubclass(http.client.IncompleteRead, ValueError))

    def test_unexpected_json_shape_is_retryable(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"[]")
        with patch.object(readiness.urllib.request, "build_opener", return_value=opener), patch.object(
                readiness, "network_snapshot", return_value=None):
            self.assertFalse(client.ready())

    def test_helper_restart_during_setup_does_not_hide_new_ethernet_connection(self):
        with patch.object(readiness, "status", return_value=None), patch.object(
                readiness, "network_snapshot", return_value={"connected": True}):
            self.assertTrue(client.ready())


class BootReadinessTest(unittest.TestCase):
    def test_healthy_ethernet_or_saved_wifi_never_opens_setup(self):
        for phase in ("checking", "setup", "error"):
            with self.subTest(phase=phase):
                self.assertEqual(readiness.choose(
                    {"connected": False, "phase": phase}, {"connected": True}, False), "online")

    def test_helper_failure_does_not_override_real_address_readiness(self):
        self.assertEqual(readiness.choose(None, {"connected": True}, False), "online")
        self.assertEqual(readiness.choose(None, {"connected": False}, True), "online")
        self.assertEqual(readiness.choose(None, None, True), "online")

    def test_setup_requires_confirmed_offline_helper_and_bounded_grace(self):
        for wifi in (True, False):
            offline = {"connected": False, "phase": "setup", "has_wifi": wifi}
            self.assertIsNone(readiness.choose(offline, {"connected": False}, False))
            self.assertEqual(readiness.choose(offline, {"connected": False}, True), "setup")
        for phase in ("checking", "connecting", "error"):
            self.assertEqual(readiness.choose(
                {"connected": False, "phase": phase}, {"connected": False}, True), "online")

    def test_delayed_dhcp_waits_without_opening_interactive_page(self):
        stopping = Mock()
        stopping.is_set.return_value = False
        progress = Mock()
        offline = ({"connected": False, "phase": "setup"}, {"connected": False})
        with patch.object(readiness, "observe", side_effect=[
            offline, offline, (None, {"connected": True})
        ]), patch.object(readiness.time, "monotonic", side_effect=[0, 1, 15, 29]):
            self.assertEqual(readiness.wait_for_mode(stopping, False, progress), "online")
        self.assertEqual(stopping.wait.call_count, 2)
        self.assertIn("Network connected", progress.call_args.args[0])

    def test_offline_timeout_requests_setup_only_after_wait(self):
        stopping = Mock()
        stopping.is_set.return_value = False
        offline = ({"connected": False, "phase": "setup"}, {"connected": False})
        with patch.object(readiness, "observe", return_value=offline), patch.object(
                readiness.time, "monotonic", side_effect=[0, 1, 31]):
            self.assertEqual(readiness.wait_for_mode(stopping, False), "setup")
        stopping.wait.assert_called_once_with(0.5)

    def test_recent_gate_online_decision_does_not_repeat_wait_or_open_setup(self):
        stopping = Mock()
        with patch.object(readiness, "cached_decision", return_value="online"), patch.object(
                readiness, "observe") as observe:
            self.assertEqual(readiness.wait_for_mode(stopping), "online")
        observe.assert_not_called()

    def test_cached_offline_decision_is_rechecked_before_launching_localhost(self):
        stopping = Mock()
        stopping.is_set.return_value = False
        with patch.object(readiness, "cached_decision", return_value="setup"), patch.object(
                readiness, "observe", return_value=(None, {"connected": True})):
            self.assertEqual(readiness.wait_for_mode(stopping), "online")

    def test_stale_or_malformed_gate_cache_is_not_authoritative(self):
        for data in ('[]', '{}', '{"at":"wrong","mode":"setup"}',
                     '{"at":0,"mode":"setup"}', '{"at":1000,"mode":"setup"}'):
            with self.subTest(data=data), patch.object(Path, "read_text", return_value=data), patch.object(
                    readiness.time, "monotonic", return_value=100):
                self.assertIsNone(readiness.cached_decision())

    def test_status_handles_truncation_and_malformed_json(self):
        opener = Mock()
        opener.open.side_effect = http.client.IncompleteRead(b"", 5117)
        with patch.object(readiness.urllib.request, "build_opener", return_value=opener):
            self.assertIsNone(readiness.status())
        for body in (b"[]", b"null", b'{"connected":"false"}', b"{"):
            opener.open.side_effect = None
            opener.open.return_value = io.BytesIO(body)
            with patch.object(readiness.urllib.request, "build_opener", return_value=opener):
                self.assertIsNone(readiness.status())

    def test_readonly_probe_has_whole_process_timeout_and_never_enables_network(self):
        with patch.object(readiness.subprocess, "run", side_effect=readiness.subprocess.TimeoutExpired(
                "probe", 2)) as run:
            self.assertIsNone(readiness.network_snapshot())
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        net = Mock()
        net.snapshot.return_value = {"connected": True, "has_wifi": True}
        with patch.object(network, "Network", return_value=net), contextlib.redirect_stdout(io.StringIO()):
            readiness.readonly_network()
        net.snapshot.assert_called_once_with()
        net.enable.assert_not_called()
        net.activate.assert_not_called()
        net.bus.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
