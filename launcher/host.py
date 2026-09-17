"""Unprivileged, fixed-service browser lifecycle and local host control."""
import os
import socket
import stat
import struct
import subprocess
from pathlib import Path

SERVICES = {
    "gfn": ("GeForce NOW", "https://play.geforcenow.com/", "chromium-profile"),
    "xbox": ("Xbox Cloud Gaming (unvalidated on Pi)", "https://www.xbox.com/play",
             "xbox-profile"),
}


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("An owned, nonsymlink directory is required")
    path.chmod(0o700)
    return path


def browser_command(service, state):
    _, url, name = SERVICES[service]  # Never accept a URL or command from a caller.
    profile = private_directory(state / name)
    command = [
        "/usr/bin/chromium", "--ozone-platform=wayland", "--use-angle=gles",
        "--user-data-dir=" + str(profile), "--kiosk", "--no-first-run",
        "--no-default-browser-check", "--disable-session-crashed-bubble",
        "--noerrdialogs", "--password-store=basic",
    ]
    if service == "gfn":
        if not Path("/opt/gfn-pi-compat/manifest.json").is_file():
            raise RuntimeError("Compatibility extension missing")
        command += ["--load-extension=/opt/gfn-pi-compat"]
    else:
        command += ["--disable-extensions"]
    return command + [url]


class Browser:
    UNIT = "cloudplay-stream.service"

    def __init__(self, state):
        self.state = private_directory(state)
        self.service = None
        # Recover an orphan after a killed/crashed Home process. The fixed user
        # unit owns the entire browser cgroup, not a reusable PID from a file.
        self.stop()

    def start(self, service):
        command = browser_command(service, self.state)
        self.stop()
        subprocess.run([
            "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
            "--unit=" + self.UNIT, "--service-type=exec",
            "--property=KillMode=control-group", "--property=TimeoutStopSec=2s",
            "--property=SendSIGKILL=yes", "--property=Restart=no",
            "--property=ExitType=cgroup", "--property=StandardOutput=null",
            "--property=StandardError=null",
            "--setenv=WAYLAND_DISPLAY=" + os.environ["WAYLAND_DISPLAY"],
            "--setenv=XDG_RUNTIME_DIR=" + os.environ["XDG_RUNTIME_DIR"],
            "--", *command,
        ], check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.service = service

    def stop(self):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", self.UNIT],
                       timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # "Unit not found" is expected on first boot / after --collect.
        if self.active():
            raise RuntimeError("Streaming browser did not stop")
        self.service = None

    def active(self):
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show", self.UNIT,
             "--property=ActiveState", "--value"], timeout=1,
            capture_output=True, text=True)
        if result.returncode != 0 or result.stdout.strip() not in (
                "inactive", "failed", "active", "activating", "deactivating", "reloading"):
            raise RuntimeError("Cannot verify browser state")
        return result.stdout.strip() in ("active", "activating", "deactivating", "reloading")

    def exited(self):
        if self.service is None:
            return False
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show", self.UNIT,
             "--property=MainPID", "--value"], timeout=1,
            capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip().isdigit():
            raise RuntimeError("Cannot verify browser process")
        # ExitType=cgroup retains helpers after the browser leader dies; return
        # Home and stop that cgroup rather than leave a blank streaming session.
        return int(result.stdout.strip()) == 0


class Control:
    """No HTTP, CORS or browser-visible token: owner-only Unix peer credentials."""
    def __init__(self, runtime):
        self.path = private_directory(runtime / "cloudplay-home") / "control.sock"
        # A live endpoint means another launcher owns this session.
        if self.path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            probe.settimeout(0.25)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                self.path.unlink()
            else:
                raise RuntimeError("Cloudplay Home is already running")
            finally:
                probe.close()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.socket.bind(str(self.path))
            self.path.chmod(0o600)
            self.socket.listen(4)
            self.socket.setblocking(False)
        except BaseException:
            self.socket.close()
            raise

    def poll(self):
        for _ in range(4):
            try:
                peer, _ = self.socket.accept()
            except BlockingIOError:
                return False
            with peer:
                peer.settimeout(0.01)
                _, uid, _ = struct.unpack("3i", peer.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                if uid != os.getuid():
                    continue
                try:
                    # One packet; truncation can never turn a long command into Home.
                    data = peer.recv(32)
                except (TimeoutError, ConnectionError):
                    continue
                if data == b"home":
                    return True
        return False

    def close(self):
        self.socket.close()
        self.path.unlink(missing_ok=True)


def request_home():
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
        peer.settimeout(0.5)
        peer.connect(str(Path(os.environ["XDG_RUNTIME_DIR"]) / "cloudplay-home/control.sock"))
        peer.sendall(b"home")
