import contextlib
import io
import json
import shutil
import sys
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


class BrowserHandoffTest(unittest.TestCase):
    def setUp(self):
        self.runtime = ROOT / "build/tests" / str(uuid.uuid4())
        self.runtime.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.runtime)

    def run_client(self, readiness, exited=False):
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
            stack.enter_context(patch.object(client, "ready", side_effect=readiness))
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
        popen, kill, execute, error = self.run_client([True, True])
        self.assertIsNone(error)
        popen.assert_not_called()
        kill.assert_not_called()
        execute.assert_called_once_with("/usr/local/bin/cloudplay-start", ["cloudplay-start"])

    def test_setup_ready_terminates_only_owned_browser_then_executes_gfn(self):
        popen, kill, execute, error = self.run_client([False] * 6 + [True])
        self.assertIsNone(error)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.args[0][-1], "http://127.0.0.1:8765/")
        self.assertEqual({call.args[0] for call in kill.call_args_list}, {123456})
        self.assertFalse((self.runtime / "cloudplay-network-profile").exists())
        execute.assert_called_once_with("/usr/local/bin/cloudplay-start", ["cloudplay-start"])

    def test_setup_browser_exit_returns_failure_to_outer_backoff_not_false_success(self):
        popen, kill, execute, error = self.run_client([False] * 7, exited=True)
        self.assertEqual(error, 1)
        execute.assert_not_called()
        self.assertFalse((self.runtime / "cloudplay-network-profile").exists())


if __name__ == "__main__":
    unittest.main()
