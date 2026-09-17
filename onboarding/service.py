#!/usr/bin/env python3
"""Network-only appliance setup. No CMS, shell API, profile access or LAN listener."""
import hmac
import io
import json
import os
import queue
import re
import secrets
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

from network import AP_ADDRESS, Network, countries, validate_wifi

PORT = 8765
STATE = Path("/var/lib/cloudplay-network")
DNS = Path("/etc/NetworkManager/dnsmasq-shared.d/cloudplay-portal.conf")
ASSETS = Path(__file__).parent


def failure_code(error):
    code = type(error).__name__
    get_name = getattr(error, "get_dbus_name", None)
    name = get_name() if callable(get_name) else None
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,150}", name):
        code += ":" + name
    return code


class Controller:
    def __init__(self, network=None, state_dir=STATE, dns=DNS, allowed=None):
        self.network = network
        self.state_dir, self.dns = state_dir, dns
        self.countries = countries() if allowed is None else allowed
        self.token = secrets.token_urlsafe(32)
        self.commands = queue.Queue(maxsize=1)
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.phone = None
        self.failed = False
        self.status = dict(phase="checking", connected=False, has_wifi=False,
                           error="", networks=[], country="", phone_ssid="", phone_password="")

    def public_status(self, local):
        with self.lock:
            data = dict(self.status)
        if not local:
            data.pop("phone_password", None)
        data["countries"] = self.countries
        return data

    def update(self, **values):
        with self.lock:
            self.status.update(values)

    def submit(self, action, data, local, delivered=None):
        if action not in ("connect", "phone") or action == "phone" and not local:
            raise ValueError("Unknown action")
        if action == "connect":
            data = validate_wifi(data, self.countries)
        elif (not isinstance(data, dict) or not isinstance(data.get("country"), str)
              or data["country"] not in self.countries):
            raise ValueError("Select your actual regulatory country")
        with self.lock:
            if self.status["phase"] != "setup" or self.status["connected"]:
                return False
            self.status.update(phase="connecting", error="")
            if delivered is None:
                delivered = threading.Event()
                delivered.set()
            self.commands.put_nowait((action, data, delivered))
        return True

    def stop_phone(self):
        try:
            self.network.stop_ap()
        finally:
            if self.phone:
                self.phone.shutdown()
                self.phone.server_close()
                self.phone = None
            self.dns.unlink(missing_ok=True)
            self.update(phone_ssid="", phone_password="")

    def start_phone(self):
        wifi = self.network.wifi()
        if not wifi:
            raise RuntimeError("No Wi-Fi interface")
        self.stop_phone()
        ssid = "Cloudplay-" + secrets.token_hex(2).upper()
        password = secrets.token_urlsafe(12)
        # Same ordering as Agora: DNS + listening portal before broadcasting the AP.
        self.dns.write_text(f"address=/#/{AP_ADDRESS}\n")
        self.phone = Portal((AP_ADDRESS, 80), self, interface=str(wifi[1]["Interface"]))
        threading.Thread(target=self.phone.serve_forever, daemon=True).start()
        try:
            self.network.activate(dict(ssid=ssid, password=password, security="wpa-psk"), hotspot=True)
        except Exception:
            self.stop_phone()
            raise
        self.update(phone_ssid=ssid, phone_password=password)

    def step(self, action, data):
        country = data["country"]
        self.network.set_country(country)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = self.state_dir / "country"
        staging = self.state_dir / "country.new"
        staging.write_text(country + "\n")
        staging.chmod(0o600)
        staging.replace(destination)
        self.update(country=country)
        if action == "phone":
            self.start_phone()
        else:
            had_phone = bool(self.phone)
            self.stop_phone()
            try:
                self.network.activate(data)
            except Exception:
                if had_phone:
                    self.start_phone()
                raise

    def run(self):
        try:
            if self.network is None:
                self.network = Network()
            self.dns.unlink(missing_ok=True)
            initial = self.network.snapshot()
            self.update(**initial)
            saved = self.state_dir / "country"
            if saved.exists() and saved.read_text().strip() in self.countries:
                country = saved.read_text().strip()
                if not initial["connected"] and initial["has_wifi"]:
                    self.network.set_country(country)
                self.update(country=country)
            if not initial["connected"]:
                self.network.enable()
            grace = time.monotonic() + 20
            last_scan = 0
            while not self.stop.is_set():
                snapshot = self.network.snapshot()
                self.update(**snapshot)
                if snapshot["connected"]:
                    if self.phone:
                        self.stop_phone()
                    self.update(phase="ready", error="")
                elif time.monotonic() >= grace:
                    with self.lock:
                        if self.status["phase"] != "connecting":
                            self.status["phase"] = "setup"
                    if (snapshot["has_wifi"] and not self.phone and self.commands.empty()
                            and time.monotonic() - last_scan > 20):
                        self.update(networks=self.network.scan())
                        last_scan = time.monotonic()
                try:
                    action, data, delivered = self.commands.get(timeout=1)
                except queue.Empty:
                    continue
                # Do not take the phone's radio away before its response has been written.
                if not delivered.wait(5):
                    data.clear()
                    self.update(phase="setup", error="Setup acknowledgement failed. Please retry.")
                    continue
                if self.stop.wait(0.25):
                    data.clear()
                    break
                try:
                    if not self.network.snapshot()["connected"]:
                        self.step(action, data)
                except Exception as error:
                    print(f"Cloudplay network setup failed ({failure_code(error)})", flush=True)
                    self.update(error="Network setup failed. Check the password, country and signal, then retry.")
                finally:
                    data.clear()
                    self.update(phase="setup")
        except Exception as error:
            print(f"Cloudplay network service unavailable ({failure_code(error)})", flush=True)
            self.failed = True
            self.update(phase="error", error="Network service unavailable. Connect Ethernet or restart the appliance.")
            self.stop.wait(5)
        finally:
            if self.network:
                try:
                    self.stop_phone()
                except Exception:
                    pass
            self.stop.set()


class Portal(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer otherwise performs reverse DNS, which can stall offline setup.
        TCPServer.server_bind(self)
        self.server_name = "Cloudplay"
        self.server_port = self.server_address[1]

    def __init__(self, address, controller, interface=None):
        self.controller = controller
        self.requests = threading.BoundedSemaphore(8)
        super().__init__(address, Handler, bind_and_activate=False)
        try:
            if interface:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
                self.socket.setsockopt(socket.IPPROTO_IP, 15, 1)  # Linux IP_FREEBIND
            self.server_bind()
            self.server_activate()
        except Exception:
            self.server_close()
            raise

    def process_request(self, request, address):
        if not self.requests.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.requests.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.requests.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Cloudplay"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Requests may contain hostile text; credentials never enter the journal.

    @property
    def local(self):
        return self.server.server_address[0] == "127.0.0.1"

    @property
    def origin(self):
        host, port = self.server.server_address
        return f"http://{host}" + (f":{port}" if port != 80 else "")

    def reply(self, code, value, content_type="application/json", headers=None):
        body = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def correct_host(self):
        return self.headers.get("Host") == self.origin.removeprefix("http://")

    def do_GET(self):
        if not self.correct_host():
            if not self.local:
                self.reply(302, b"", "text/plain", {"Location": self.origin + "/"})
            else:
                self.reply(403, {"error": "Host rejected"})
            return
        if self.path == "/api/status":
            self.reply(200, self.server.controller.public_status(self.local))
        elif self.path == "/api/token":
            self.reply(200, {"token": self.server.controller.token})
        elif self.path == "/phone.svg" and self.local:
            state = self.server.controller.public_status(True)
            if not state["phone_password"]:
                self.reply(404, {})
                return
            import qrcode
            import qrcode.image.svg
            qr = qrcode.make(f"WIFI:T:WPA;S:{state['phone_ssid']};P:{state['phone_password']};;",
                             image_factory=qrcode.image.svg.SvgPathImage)
            output = io.BytesIO()
            qr.save(output)
            self.reply(200, output.getvalue(), "image/svg+xml")
        elif self.path in ("/", "/setup.js", "/setup.css"):
            filename, mime = {"/": ("setup.html", "text/html; charset=utf-8"),
                              "/setup.js": ("setup.js", "text/javascript"),
                              "/setup.css": ("setup.css", "text/css")}[self.path]
            self.reply(200, (ASSETS / filename).read_bytes(), mime)
        elif not self.local:
            # Apple/Android/Windows captive probes all lead to the same setup origin.
            self.reply(302, b"", "text/plain", {"Location": self.origin + "/"})
        else:
            self.reply(404, {})

    def do_POST(self):
        controller = self.server.controller
        if (not self.correct_host() or self.headers.get("Origin") != self.origin
                or not hmac.compare_digest(self.headers.get("X-Cloudplay-Token", "").encode(),
                                           controller.token.encode())):
            self.reply(403, {"error": "Request rejected"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get("Content-Type") != "application/json":
                raise ValueError("Invalid request")
            data = json.loads(self.rfile.read(length))
            action = {"/api/connect": "connect", "/api/phone": "phone"}.get(self.path, "")
            delivered = threading.Event()
            accepted = controller.submit(action, data, self.local, delivered)
            self.reply(202 if accepted else 409, {"accepted": accepted})
            delivered.set()
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "Check the network, password format and country."})


def main():
    if os.geteuid() != 0:
        raise SystemExit("Network helper requires its system service")
    controller = Controller()
    portal = Portal(("127.0.0.1", PORT), controller)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: controller.stop.set())
    worker = threading.Thread(target=controller.run, daemon=True)
    worker.start()
    portal.timeout = 1
    while not controller.stop.is_set():
        portal.handle_request()
    portal.server_close()
    worker.join(timeout=60)
    if worker.is_alive() or controller.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
