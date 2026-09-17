import http.client
import json
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
import network
import service


class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "build" / "tests" / str(uuid.uuid4())
        self.work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.work)

    def controller(self, net=None):
        return service.Controller(net or Mock(), self.work / "state",
                                  self.work / "portal.conf", {"US": "United States", "CA": "Canada"})


class ValidationTest(unittest.TestCase):
    def data(self, **changes):
        return dict(ssid="Router", password="valid-password", security="wpa-psk",
                    country="US", hidden=False, **changes)

    def test_special_ssids_preserved_not_shell_interpreted(self):
        data = self.data()
        data["ssid"] = 'A:B\\C "D";$(x)'
        self.assertEqual(network.validate_wifi(data, {"US"})["ssid"], data["ssid"])

    def test_oversize_multibyte_ssid_and_malformed_types_rejected(self):
        for key, value in (("ssid", "é" * 17), ("ssid", ""), ("ssid", "bad\0name"),
                           ("password", []), ("country", []), ("country", "ZZ"),
                           ("security", []), ("hidden", "false"), ("password", "short")):
            data = self.data()
            data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                network.validate_wifi(data, {"US"})

    def test_open_and_wpa3_validation(self):
        data = self.data()
        data.update(security="open", password="")
        self.assertEqual(network.validate_wifi(data, {"US"})["security"], "open")
        data.update(security="sae", password="x")
        self.assertEqual(network.validate_wifi(data, {"US"})["security"], "sae")
        data.update(security="wpa-psk", password="a" * 64)
        network.validate_wifi(data, {"US"})
        data["password"] = "g" * 64
        with self.assertRaises(ValueError):
            network.validate_wifi(data, {"US"})

    def test_security_modes_and_enterprise_not_mislabeled_open(self):
        for flags, wpa, rsn, expected in (
            (0, 0, 0, "open"), (1, 0, 0x100, "wpa-psk"),
            (1, 0, 0x400, "sae"), (1, 0, 0x500, "wpa-psk"),
            (0, 0, 0x800, "owe"), (1, 0, 0x200, "unsupported"), (1, 0, 0, "unsupported"),
        ):
            self.assertEqual(network.security_mode(flags, wpa, rsn), expected)

    def test_link_local_and_loopback_are_not_dhcp_success(self):
        for address in ("127.0.0.1", "::1", "169.254.1.2", "fe80::1", "0.0.0.0", "garbage"):
            self.assertFalse(network.usable_address(address), address)
        for address in ("192.168.1.50", "10.0.0.3", "fd12:3456::2"):
            self.assertTrue(network.usable_address(address), address)


class NetworkTest(unittest.TestCase):
    def net(self):
        net = network.Network.__new__(network.Network)
        net.dbus = SimpleNamespace(
            Dictionary=lambda data, **_: dict(data), Array=lambda data, **_: list(data),
            ByteArray=bytes, UInt32=int, Int32=int, Boolean=bool, ObjectPath=str,
        )
        net.ap_connection = None
        net.wifi = Mock(return_value=("/device/wifi", {"Interface": "wlan0"}))
        net.call = Mock(return_value=("/new-connection", "/active", {}))
        net.properties = Mock(side_effect=[{"State": 2}, {"State": 100}])
        net.device_ready = Mock(return_value=True)
        return net

    def test_password_goes_over_dbus_not_process_argv_and_save_follows_dhcp(self):
        net = self.net()
        data = dict(ssid='Network:"', password="secret-password", security="wpa-psk", hidden=True)
        with patch.object(network.subprocess, "run") as process:
            net.activate(data)
        process.assert_not_called()
        args = net.call.call_args_list[0].args
        self.assertEqual(args[2], "AddAndActivateConnection2")
        self.assertEqual(args[3]["802-11-wireless"]["ssid"], b'Network:"')
        self.assertEqual(args[3]["802-11-wireless-security"]["psk"], data["password"])
        self.assertEqual(args[6]["persist"], "memory")
        self.assertEqual(net.call.call_args_list[-1].args,
                         ("/new-connection", network.NM + ".Settings.Connection", "Save"))

    def test_hotspot_is_volatile_and_bound_to_service_bus_lifetime(self):
        net = self.net()
        net.activate(dict(ssid="Cloudplay-1234", password="unique-password", security="wpa-psk"), True)
        args = net.call.call_args.args
        self.assertEqual(args[6], {"persist": "volatile", "bind-activation": "dbus-client"})
        self.assertFalse(args[3]["connection"]["autoconnect"])
        self.assertEqual(args[3]["ipv4"]["method"], "shared")
        self.assertEqual(net.ap_connection, "/new-connection")
        self.assertEqual(net.call.call_count, 1)

    def test_failed_activation_deletes_only_new_profile(self):
        net = self.net()
        net.properties = Mock(return_value={"State": 4})
        with self.assertRaises(RuntimeError):
            net.activate(dict(ssid="SSID", password="wrong-password", security="wpa-psk"))
        self.assertEqual(net.call.call_args.args,
                         ("/new-connection", network.NM + ".Settings.Connection", "Delete"))

    def test_activated_without_dhcp_is_not_saved(self):
        net = self.net()
        net.device_ready.return_value = False
        with patch.object(network.time, "monotonic", side_effect=[0, 0, 46]), \
             patch.object(network.time, "sleep"), self.assertRaises(RuntimeError):
            net.activate(dict(ssid="SSID", password="password", security="wpa-psk"))
        self.assertFalse(any(call.args[2] == "Save" for call in net.call.call_args_list))
        self.assertEqual(net.call.call_args.args[2], "Delete")

    def test_hotspot_address_is_not_connected_network(self):
        net = network.Network.__new__(network.Network)
        net.properties = Mock(return_value={"Mode": 3})
        self.assertFalse(net.device_ready("/wifi", {"State": 100, "DeviceType": 2}))
        self.assertEqual(net.properties.call_count, 1)

    def test_ipv6_ready_and_pending_dhcp(self):
        net = network.Network.__new__(network.Network)
        net.properties = Mock(return_value={"AddressData": [{"address": "fd12:3456::2"}]})
        props = {"State": 100, "DeviceType": 1, "Ip4Config": "/", "Ip6Config": "/ip6"}
        self.assertTrue(net.device_ready("/ethernet", props))
        props["State"] = 70
        self.assertFalse(net.device_ready("/ethernet", props))


class ControllerTest(ScratchTest):
    def test_connected_and_busy_sessions_reject_mutation(self):
        controller = self.controller()
        valid = dict(ssid="SSID", password="password", security="wpa-psk", country="US")
        self.assertFalse(controller.submit("connect", valid, True))
        controller.update(phase="setup", connected=True)
        self.assertFalse(controller.submit("connect", valid, True))
        controller.update(connected=False)
        self.assertTrue(controller.submit("connect", valid, True))
        self.assertFalse(controller.submit("connect", valid, True))
        self.assertEqual(controller.commands.qsize(), 1)

    def test_phone_cannot_create_another_hotspot_or_read_its_password(self):
        controller = self.controller()
        controller.update(phase="setup", phone_password="private")
        with self.assertRaises(ValueError):
            controller.submit("phone", {"country": "US"}, False)
        self.assertNotIn("phone_password", controller.public_status(False))
        self.assertEqual(controller.public_status(True)["phone_password"], "private")

    def test_dns_and_listener_precede_ap_and_cleanup_is_owned(self):
        events = []
        controller = self.controller()
        controller.network.wifi.return_value = ("/wifi", {"Interface": "wlan0"})
        controller.network.activate.side_effect = lambda *_a, **_k: events.append("activate")
        class Listener:
            def __init__(self, address, owner, interface):
                self.assertions = (address, interface)
                events.append("listen")
                assert owner.dns.read_text() == "address=/#/10.42.0.1\n"
            def serve_forever(self):
                pass
            def shutdown(self):
                events.append("shutdown")
            def server_close(self):
                events.append("close")
        with patch.object(service, "Portal", Listener):
            controller.start_phone()
        self.assertLess(events.index("listen"), events.index("activate"))
        self.assertTrue(controller.status["phone_password"])
        controller.stop_phone()
        self.assertFalse(controller.dns.exists())
        self.assertIsNone(controller.phone)

    def test_ap_failure_removes_listener_and_dns(self):
        controller = self.controller()
        controller.network.wifi.return_value = ("/wifi", {"Interface": "wlan0"})
        controller.network.activate.side_effect = RuntimeError("activation failed")
        with patch.object(service, "Portal") as portal:
            with self.assertRaises(RuntimeError):
                controller.start_phone()
            portal.return_value.server_close.assert_called_once()
        self.assertFalse(controller.dns.exists())
        self.assertIsNone(controller.phone)

    def test_existing_ethernet_skips_scan_ap_and_cms(self):
        controller = self.controller()
        controller.network.snapshot.return_value = {"connected": True, "has_wifi": True}
        def finish(**_):
            controller.stop.set()
            raise service.queue.Empty
        controller.commands.get = Mock(side_effect=finish)
        controller.run()
        self.assertEqual(controller.status["phase"], "ready")
        controller.network.activate.assert_not_called()
        controller.network.scan.assert_not_called()

    def test_failed_phone_connection_returns_to_phone_setup(self):
        controller = self.controller()
        controller.phone = Mock()
        controller.network.activate.side_effect = RuntimeError("bad password")
        with patch.object(controller, "stop_phone") as stop, patch.object(controller, "start_phone") as start:
            with self.assertRaises(RuntimeError):
                controller.step("connect", dict(country="CA", ssid="SSID", password="wrong",
                                                security="wpa-psk"))
        stop.assert_called_once()
        start.assert_called_once()
        self.assertEqual((controller.state_dir / "country").read_text(), "CA\n")

    def test_no_wifi_waits_for_ethernet_without_starting_hotspot_or_scanning(self):
        controller = self.controller()
        controller.network.snapshot.return_value = {"connected": False, "has_wifi": False}
        def finish(**_):
            controller.stop.set()
            raise service.queue.Empty
        controller.commands.get = Mock(side_effect=finish)
        with patch.object(service.time, "monotonic", side_effect=[0, 21]):
            controller.run()
        self.assertFalse(controller.failed)
        self.assertEqual(controller.status["phase"], "setup")
        controller.network.activate.assert_not_called()
        controller.network.scan.assert_not_called()

    def test_failed_http_acknowledgement_never_changes_radio(self):
        controller = self.controller()
        controller.network.snapshot.return_value = {"connected": False, "has_wifi": False}
        acknowledgement = Mock()
        acknowledgement.wait.return_value = False
        data = {"country": "US"}
        def commands(**_):
            if data:
                return "phone", data, acknowledgement
            controller.stop.set()
            raise service.queue.Empty
        controller.commands.get = Mock(side_effect=commands)
        controller.run()
        self.assertEqual(data, {})
        self.assertIn("acknowledgement failed", controller.status["error"])
        controller.network.activate.assert_not_called()


class PortalTest(ScratchTest):
    def setUp(self):
        super().setUp()
        self.control = self.controller()
        self.control.update(phase="setup", has_wifi=True)
        self.server = service.Portal(("127.0.0.1", 0), self.control)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.headers), response.read()

    def headers(self):
        return {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}",
                "X-Cloudplay-Token": self.control.token}

    def test_ui_and_no_cache_csp(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Network setup", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_listener_does_not_require_reverse_dns(self):
        with patch.object(service.socket, "getfqdn", side_effect=AssertionError("Offline DNS must not be needed")):
            server = service.Portal(("127.0.0.1", 0), self.control)
            server.server_close()

    def test_dns_rebinding_wrong_origin_and_missing_token_rejected(self):
        status, _, _ = self.request("GET", "/api/token", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        for key, value in (("Origin", "https://play.geforcenow.com"),
                           ("X-Cloudplay-Token", ""), ("X-Cloudplay-Token", "é"),
                           ("Host", "evil.example")):
            headers = self.headers()
            headers[key] = value
            status, _, _ = self.request("POST", "/api/phone", {"country": "US"}, headers)
            self.assertEqual(status, 403)
        self.assertTrue(self.control.commands.empty())

    def test_valid_local_connect_and_no_password_echo(self):
        data = dict(ssid="SSID", security="wpa-psk", password="sensitive-password", country="US")
        status, _, body = self.request("POST", "/api/connect", data, self.headers())
        self.assertEqual(status, 202)
        self.assertNotIn(b"sensitive-password", body)
        self.assertEqual(self.control.commands.get_nowait()[0], "connect")

    def test_oversized_and_malformed_requests_rejected(self):
        for data in (["bad"], {"country": []}, {"ssid": "x" * 5000}):
            status, _, _ = self.request("POST", "/api/connect", data, self.headers())
            self.assertEqual(status, 400)
        status, _, _ = self.request("GET", "/../network.py")
        self.assertEqual(status, 404)

    def test_phone_probes_redirect_and_phone_never_receives_hotspot_password(self):
        self.server.server_address = ("10.42.0.1", 80)
        self.control.update(phone_password="local-screen-only")
        status, headers, _ = self.request("GET", "/generate_204", headers={"Host": "captive.example"})
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://10.42.0.1/")
        status, _, body = self.request("GET", "/api/status", headers={"Host": "10.42.0.1"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"local-screen-only", body)
        self.assertNotIn(b"phone_password", body)
        headers = self.headers()
        headers.update(Host="10.42.0.1", Origin="http://10.42.0.1")
        status, _, _ = self.request("POST", "/api/phone", {"country": "US"}, headers)
        self.assertEqual(status, 400)


class WiringTest(unittest.TestCase):
    def test_no_cms_or_framebuffer_runtime_and_sandboxed_separate_profile(self):
        client = (ROOT / "onboarding/client.py").read_text()
        self.assertIn("cloudplay-network-profile", client)
        self.assertIn("XDG_RUNTIME_DIR", client)
        self.assertNotIn("--no-sandbox", client)
        self.assertNotIn("--remote-debugging", client)
        self.assertIn("start_new_session=True", client)
        self.assertIn('os.execv("/usr/local/bin/cloudplay-start"', client)
        for file in (ROOT / "onboarding").glob("*.py"):
            self.assertNotIn("/dev/fb0", file.read_text())
            self.assertNotIn("agora-cms", file.read_text())

    def test_service_cannot_read_nvidia_profile_and_portal_is_interface_bound(self):
        files = ROOT / "stage-cloudplay/00-appliance/files"
        unit = (files / "cloudplay-network.service").read_text()
        self.assertIn("ProtectHome=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        server = (ROOT / "onboarding/service.py").read_text()
        self.assertIn("SO_BINDTODEVICE", server)
        self.assertIn('Portal(("127.0.0.1", PORT)', server)
        self.assertNotIn('"0.0.0.0"', server)
        self.assertIn("onboarding/client.py", (files / "cloudplay-browser-session").read_text())


if __name__ == "__main__":
    unittest.main()
