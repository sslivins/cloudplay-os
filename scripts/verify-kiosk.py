#!/usr/bin/env python3
"""Fail the image export if the kiosk becomes a desktop or loses its boot wiring."""
import hashlib
import json
import configparser
import stat
import re
import subprocess
import tomllib
import ctypes
import hmac
import os
from pathlib import Path
from xml.etree import ElementTree


def effective_journal_settings(text):
    config = configparser.ConfigParser(strict=False, interpolation=None)
    config.optionxform = str
    config.read_string(text)
    return dict(config["Journal"])


def verify_boot_order():
    result = subprocess.run(
        ["systemd-analyze", "verify", "--man=no", "graphical.target"],
        capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 0, diagnostics
    # systemd can report success after deleting jobs to resolve a boot cycle.
    assert "ordering cycle" not in diagnostics.lower(), diagnostics
    return {"target": "graphical.target", "ordering_cycles": False}


def home_directory_metadata(home, uid, gid):
    metadata = {}
    for path in (home, home / ".config", home / ".config/cloudplay"):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode), path
        assert info.st_uid == uid and info.st_gid == gid, path
        assert stat.S_IMODE(info.st_mode) == 0o700, path
        metadata[str(path)] = {"uid": uid, "gid": gid, "mode": "0700"}
    return metadata


def development_password_matches(stored):
    if not stored or stored.startswith(("!", "*")):
        return False
    library = ctypes.CDLL("libcrypt.so.1")
    library.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    library.crypt.restype = ctypes.c_char_p
    result = library.crypt(b"cloud", stored.encode())
    return bool(result and hmac.compare_digest(result, stored.encode()))


def ssh_settings(text):
    settings = {}
    for line in text.splitlines():
        if " " in line:
            key, value = line.split(" ", 1)
            settings[key] = settings[key] + " " + value if key in settings else value
    return settings


def effective_ssh(command="/usr/sbin/sshd", extra=()):
    # pi-gen removes host keys. Validate with a disposable in-memory key rather
    # than generating a shared host identity that might leak into an image.
    key = subprocess.check_output(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        stderr=subprocess.DEVNULL)
    fd = os.memfd_create("cloudplay-ssh-validation", os.MFD_CLOEXEC)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(os.dup(fd), "wb") as output:
            output.write(key)
        os.lseek(fd, 0, os.SEEK_SET)
        # sshd closes inherited extra descriptors at startup; open our parent's
        # still-live descriptor instead, without placing a private key on disk.
        return ssh_settings(subprocess.check_output(
            [command, "-T", "-h", f"/proc/{os.getpid()}/fd/{fd}", *extra,
             "-C", "user=cloud,host=localhost,addr=127.0.0.1"], text=True))
    finally:
        os.close(fd)


def main():
    import pwd
    installed = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n"], text=True)
    names = {line.split("\t")[0].split(":")[0] for line in installed.splitlines()
             if line.endswith("\tinstalled")}
    forbidden = {"lightdm", "piwiz", "userconf-pi", "rpd-wayland-core", "rpd-x-core",
                 "rpd-common", "wf-panel-pi", "pcmanfm-pi", "lxpanel", "lxsession",
                 "raspberrypi-ui-mods", "rpi-connect-lite"}
    assert not names & forbidden, f"Desktop/wizard packages installed: {names & forbidden}"
    required = {"greetd", "openssh-server", "openssl", "sudo", "labwc", "swaybg", "libpam-systemd", "dbus-user-session",
                "pipewire", "pipewire-pulse", "wireplumber", "plymouth", "plymouth-themes",
                "dnsmasq-base", "python3-dbus", "python3-qrcode", "iw", "rfkill",
                "wpasupplicant", "wireless-regdb", "python3-gi", "gir1.2-gtk-3.0"}
    assert required <= names, f"Missing kiosk packages: {required - names}"
    boot_order = verify_boot_order()
    home_smoke = json.loads(Path("/usr/local/share/cloudplay/home-smoke.json").read_text())
    assert home_smoke == {"passed": True}, home_smoke
    account = pwd.getpwnam("cloudplay")
    assert account.pw_uid == 1000 and account.pw_shell == "/bin/bash"
    home_metadata = home_directory_metadata(Path(account.pw_dir), account.pw_uid, account.pw_gid)
    shadow = {line.split(":")[0]: line.split(":")[1] for line in Path("/etc/shadow").read_text().splitlines()}
    assert all(shadow[name].startswith(("!", "*")) for name in ("root", "cloudplay"))
    assert "rpi-first-boot-wizard" not in shadow
    groups = subprocess.check_output(["id", "-nG", "cloudplay"], text=True).split()
    assert not set(groups) & {"sudo", "adm", "disk", "input"}
    assert "cloudplay-gamepad" in groups
    config = tomllib.loads(Path("/etc/greetd/config.toml").read_text())
    for name in ("initial_session", "default_session"):
        assert config[name]["user"] == "cloudplay"
        assert "/usr/local/bin/cloudplay-session" in config[name]["command"]
        assert "agreety" not in config[name]["command"]
    assert config["terminal"]["vt"] == 7
    assert "@include login" in Path("/etc/pam.d/greetd").read_text()
    assert "pam_systemd.so" in Path("/etc/pam.d/common-session").read_text()
    assert Path("/etc/systemd/system/display-manager.service").resolve().name == "greetd.service"
    assert Path("/etc/systemd/system/default.target").resolve().name == "graphical.target"
    flag = Path("/etc/cloudplay/development-ssh").read_text().strip()
    assert flag in ("0", "1")
    development_ssh = flag == "1"
    assert not list(Path("/etc/ssh").glob("ssh_host_*_key")), "Image contains a shared SSH host key"
    keygen = Path("/usr/lib/systemd/system/regenerate_ssh_host_keys.service").read_text()
    assert "ConditionFirstBoot=yes" in keygen and "ssh-keygen -A" in keygen
    assert Path("/etc/systemd/system/sysinit.target.wants/regenerate_ssh_host_keys.service").is_symlink()
    ssh = effective_ssh()
    assert ssh["permitrootlogin"] == "no"
    assert {"root", "cloudplay"} <= set(ssh["denyusers"].split())
    assert ssh["passwordauthentication"] == ("yes" if development_ssh else "no")
    assert ssh["authenticationmethods"] == "any" and ssh["usepam"] == "yes"
    if development_ssh:
        admin = pwd.getpwnam("cloud")
        assert admin.pw_uid > 1000 and admin.pw_shell == "/bin/bash"
        assert development_password_matches(shadow["cloud"]), "Public development password mismatch"
        assert "sudo" in subprocess.check_output(["id", "-nG", "cloud"], text=True).split()
        sudoers = Path("/etc/sudoers.d/90-cloudplay-development")
        assert sudoers.read_text().strip() == "cloud ALL=(ALL:ALL) PASSWD: ALL"
        assert stat.S_IMODE(sudoers.stat().st_mode) == 0o440
        subprocess.run(["visudo", "-cf", str(sudoers)], check=True)
        effective_sudo = subprocess.check_output(["sudo", "-l", "-U", "cloud"], text=True)
        assert "NOPASSWD:" not in effective_sudo and "PASSWD: ALL" in effective_sudo
        assert subprocess.check_output(["systemctl", "is-enabled", "ssh.service"], text=True).strip() == "enabled"
    else:
        assert str(Path("/etc/systemd/system/ssh.service").resolve()) == "/dev/null"
        assert "cloud" not in shadow or shadow["cloud"].startswith(("!", "*"))
    for path in ("/etc/cloud/cloud.cfg.d/zz-cloudplay.cfg", "/boot/firmware/user-data"):
        assert "ssh_pwauth: " + str(development_ssh).lower() in Path(path).read_text()
    for service in ("ssh.socket", "userconfig.service", "getty@.service", "serial-getty@.service"):
        assert Path("/etc/systemd/system", service).is_symlink()
        assert str(Path("/etc/systemd/system", service).resolve()) == "/dev/null"
    labwc = ElementTree.parse("/etc/cloudplay/labwc/rc.xml")
    home_binding = labwc.find("./keyboard/keybind[@key='C-A-Home']")
    assert home_binding is not None and home_binding.get("overrideInhibition") == "yes"
    assert home_binding.get("onRelease") == "yes"
    binding = labwc.find("./keyboard/keybind[@key='C-A-Home']/action")
    assert binding is not None and binding.attrib == {
        "name": "Execute", "command": "/usr/local/bin/cloudplay-home"}
    assert "lxsession" not in Path("/etc/cloudplay/labwc/autostart").read_text()
    launcher = Path("/usr/local/bin/cloudplay-start").read_text()
    assert "launcher/main.py" in launcher
    for name in ("cloudplay-logo.png", "keyboard-icon.png", "controller-icon.png"):
        asset = Path("/usr/local/lib/cloudplay/launcher/assets", name)
        assert asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    launcher = Path("/usr/local/lib/cloudplay/launcher/host.py").read_text()
    assert "--kiosk" in launcher and "https://play.geforcenow.com/" in launcher
    assert "https://www.xbox.com/play" in launcher and "chromium-profile" in launcher
    assert "KillMode=control-group" in launcher and "TimeoutStopSec=2s" in launcher
    assert "--no-sandbox" not in launcher and "--remote-debugging" not in launcher
    gamepad_rule = Path("/etc/udev/rules.d/71-cloudplay-gamepad.rules").read_text()
    for condition in ('ENV{ID_INPUT_JOYSTICK}=="1"', 'ENV{ID_INPUT_KEYBOARD}!="1"',
                      'GROUP="cloudplay-gamepad"', 'MODE="0660"', 'KERNEL=="js*"'):
        assert condition in gamepad_rule
    subprocess.run(["udevadm", "verify", "/etc/udev/rules.d/71-cloudplay-gamepad.rules"],
                   check=True, capture_output=True, text=True)
    assert "onboarding/client.py" in Path("/usr/local/bin/cloudplay-browser-session").read_text()
    network_unit = Path("/etc/systemd/system/cloudplay-network.service").read_text()
    for setting in ("ProtectHome=yes", "ProtectSystem=strict", "NoNewPrivileges=yes",
                    "StateDirectoryMode=0700"):
        assert setting in network_unit
    diagnostics = Path("/etc/systemd/journald.conf.d/cloudplay-diagnostics.conf").read_text()
    for setting in ("Storage=persistent", "SystemMaxUse=64M", "SyncIntervalSec=15s"):
        assert setting in diagnostics
    assert Path("/var/log/journal").is_dir()
    journal = effective_journal_settings(subprocess.check_output(
        ["systemd-analyze", "cat-config", "systemd/journald.conf"], text=True))
    assert journal["Storage"] == "persistent", journal
    assert journal["SystemMaxUse"] == "64M" and journal["SyncIntervalSec"] == "15s", journal
    for unit in ("cloudplay-network.service", "cloudplay-wifi-radio.service",
                 "cloudplay-startup.service"):
        assert Path("/etc/systemd/system/multi-user.target.wants", unit).is_symlink()
    startup = Path("/etc/systemd/system/cloudplay-startup.service").read_text()
    assert "Before=plymouth-quit.service plymouth-quit-wait.service greetd.service" in startup
    assert "TimeoutStartSec=40" in startup
    assert "swaybg" in Path("/etc/cloudplay/labwc/autostart").read_text()
    cmdline = Path("/boot/firmware/cmdline.txt").read_text().split()
    assert {"quiet", "splash", "console=tty3", "vt.global_cursor_default=0"} <= set(cmdline)
    assert "console=tty1" not in cmdline
    assert "auto_initramfs=1" in Path("/boot/firmware/config.txt").read_text()
    assert subprocess.check_output(["plymouth-set-default-theme"], text=True).strip() == "cloudplay"
    artwork = Path("/usr/share/plymouth/themes/cloudplay/cloudplay.png")
    assert hashlib.sha256(artwork.read_bytes()).hexdigest() == "b2d7338996250ec4f9a34a8534d8bb9d04da7fa5e6305e431633fb184b57ae0b"
    theme = artwork.parent
    assert hashlib.sha256((theme / "cloudplay-background.png").read_bytes()).hexdigest() == "63436f2ffa432dd5461165b47e443c670c60c6968020736d3a4b69ce39257399"
    assert 'Image("cloudplay-background.png")' in (theme / "cloudplay.script").read_text()
    frames = [theme / f"spinner-{index:02}.png" for index in range(12)]
    assert len({hashlib.sha256(path.read_bytes()).digest() for path in frames}) == 12
    initramfs = {}
    for path in sorted(Path("/boot").glob("initrd.img-*")):
        listing = subprocess.check_output(["lsinitramfs", str(path)], text=True)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.script" in listing, str(path)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.plymouth" in listing, str(path)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.png" in listing, str(path)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay-background.png" in listing, str(path)
        for frame in frames:
            assert str(frame).lstrip("/") in listing, str(path)
        assert re.search(r"/label-pango\.so$", listing, re.MULTILINE), str(path)
        assert re.search(r"/script\.so$", listing, re.MULTILINE), str(path)
        assert re.search(r"/vc4\.ko(?:\.\w+)?$", listing, re.MULTILINE), str(path)
        initramfs[path.name] = {"cloudplay_theme_embedded": True, "script_plugin": True, "vc4_module": True}
    assert initramfs, "No initramfs found"
    paths = [
        "/etc/greetd/config.toml", "/etc/systemd/system/greetd.service.d/cloudplay.conf",
        "/etc/cloudplay/labwc/rc.xml", "/etc/cloudplay/labwc/autostart",
        "/usr/local/bin/cloudplay-start", "/usr/local/bin/cloudplay-session",
        "/usr/local/bin/cloudplay-browser-session", "/usr/local/lib/cloudplay/supervise.py",
        "/usr/local/bin/cloudplay-home", "/etc/udev/rules.d/71-cloudplay-gamepad.rules",
        "/usr/local/share/cloudplay/home-smoke.json",
        "/usr/local/share/cloudplay/SECURITY.txt",
        "/opt/cloudplay-build-inputs/check-launcher.py",
        "/usr/share/plymouth/themes/cloudplay/cloudplay.script",
        "/usr/share/plymouth/themes/cloudplay/cloudplay.plymouth",
        "/usr/share/plymouth/themes/cloudplay/cloudplay.png",
        "/usr/share/plymouth/themes/cloudplay/cloudplay-background.png",
        "/boot/firmware/cmdline.txt", "/boot/firmware/config.txt",
        "/etc/systemd/system/cloudplay-network.service",
        "/etc/systemd/system/cloudplay-wifi-radio.service",
        "/etc/systemd/system/cloudplay-startup.service",
        "/etc/systemd/journald.conf.d/cloudplay-diagnostics.conf",
        "/usr/local/bin/cloudplay-development-ssh",
        "/etc/cloudplay/development-ssh", "/etc/ssh/sshd_config.d/00-cloudplay-access.conf",
    ]
    paths += ["/usr/local/lib/cloudplay/onboarding/" + name for name in
              ("network.py", "service.py", "client.py", "readiness.py", "boot.py",
               "setup.html", "setup.js", "setup.css")]
    paths += ["/usr/local/lib/cloudplay/launcher/" + name for name in
              ("main.py", "host.py", "gamepad.py")]
    paths += ["/usr/local/lib/cloudplay/launcher/assets/" + name for name in
              ("cloudplay-logo.png", "keyboard-icon.png", "controller-icon.png")]
    paths += [str(path) for path in frames]
    if development_ssh:
        paths.append("/etc/sudoers.d/90-cloudplay-development")
    for path in paths:
        info = Path(path).stat()
        assert info.st_uid == 0 and not info.st_mode & 0o022, path
    report = {
        "design": "lite-wayland-kiosk", "account": "cloudplay", "uid": 1000,
        "browser_and_root_passwords_locked": True, "ssh_masked": not development_ssh,
        "development_ssh": development_ssh, "root_and_browser_ssh_denied": True,
        "device_unique_host_keys": "generated on first boot; no private host key in image",
        "ssh_effective_policy": {key: ssh[key] for key in
                                 ("permitrootlogin", "denyusers", "passwordauthentication",
                                  "authenticationmethods", "usepam")},
        "development_login": "cloud / cloud (public, temporary)" if development_ssh else None,
        "desktop_wizard_absent": True,
        "session": "greetd PAM/login + logind + dbus-run-session + labwc",
        "browser_release": "v0.4.1", "plymouth_theme": "cloudplay",
        "onboarding": "network-only; separate sandboxed setup browser; private optional phone AP",
        "home": "native GTK Wayland; owner-only Unix control; fixed per-service user cgroup",
        "home_ui_smoke": home_smoke,
        "services": {"gfn": "existing persistent profile", "xbox": "separate persistent profile"},
        "controller": "up to four classified non-keyboard gamepads; Select+Start hold 2s",
        "startup_gate": "bounded DHCP wait before Plymouth releases DRM; helper failure does not force setup",
        "boot_order": boot_order,
        "journal_effective_settings": journal,
        "home_directories": home_metadata,
        "initramfs": initramfs, "boot_validated": False,
        "configuration_sha256": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
    }
    Path("/usr/local/share/cloudplay/kiosk-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
