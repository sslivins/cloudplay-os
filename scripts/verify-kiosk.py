#!/usr/bin/env python3
"""Fail the image export if the kiosk becomes a desktop or loses its boot wiring."""
import hashlib
import json
import pwd
import re
import subprocess
import tomllib
from pathlib import Path
from xml.etree import ElementTree


def main():
    installed = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n"], text=True)
    names = {line.split("\t")[0].split(":")[0] for line in installed.splitlines()
             if line.endswith("\tinstalled")}
    forbidden = {"lightdm", "piwiz", "userconf-pi", "rpd-wayland-core", "rpd-x-core",
                 "rpd-common", "wf-panel-pi", "pcmanfm-pi", "lxpanel", "lxsession",
                 "raspberrypi-ui-mods", "rpi-connect-lite"}
    assert not names & forbidden, f"Desktop/wizard packages installed: {names & forbidden}"
    required = {"greetd", "labwc", "libpam-systemd", "dbus-user-session",
                "pipewire", "pipewire-pulse", "wireplumber", "plymouth", "plymouth-themes"}
    assert required <= names, f"Missing kiosk packages: {required - names}"
    account = pwd.getpwnam("cloudplay")
    assert account.pw_uid == 1000 and account.pw_shell == "/bin/bash"
    shadow = {line.split(":")[0]: line.split(":")[1] for line in Path("/etc/shadow").read_text().splitlines()}
    assert all(shadow[name].startswith(("!", "*")) for name in ("root", "cloudplay"))
    assert "rpi-first-boot-wizard" not in shadow
    groups = subprocess.check_output(["id", "-nG", "cloudplay"], text=True).split()
    assert not set(groups) & {"sudo", "adm", "disk"}
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
    for service in ("ssh.service", "ssh.socket", "userconfig.service", "getty@.service", "serial-getty@.service"):
        assert Path("/etc/systemd/system", service).is_symlink()
        assert str(Path("/etc/systemd/system", service).resolve()) == "/dev/null"
    ElementTree.parse("/etc/cloudplay/labwc/rc.xml")
    assert "lxsession" not in Path("/etc/cloudplay/labwc/autostart").read_text()
    launcher = Path("/usr/local/bin/cloudplay-start").read_text()
    assert "--kiosk" in launcher and "https://play.geforcenow.com/" in launcher
    assert "--no-sandbox" not in launcher and "--remote-debugging" not in launcher
    cmdline = Path("/boot/firmware/cmdline.txt").read_text().split()
    assert {"quiet", "splash", "console=tty3", "vt.global_cursor_default=0"} <= set(cmdline)
    assert "console=tty1" not in cmdline
    assert "auto_initramfs=1" in Path("/boot/firmware/config.txt").read_text()
    assert subprocess.check_output(["plymouth-set-default-theme"], text=True).strip() == "cloudplay"
    initramfs = {}
    for path in sorted(Path("/boot").glob("initrd.img-*")):
        listing = subprocess.check_output(["lsinitramfs", str(path)], text=True)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.script" in listing, str(path)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.plymouth" in listing, str(path)
        assert re.search(r"/script\.so$", listing, re.MULTILINE), str(path)
        assert re.search(r"/vc4\.ko(?:\.\w+)?$", listing, re.MULTILINE), str(path)
        initramfs[path.name] = {"cloudplay_theme_embedded": True, "script_plugin": True, "vc4_module": True}
    assert initramfs, "No initramfs found"
    paths = [
        "/etc/greetd/config.toml", "/etc/systemd/system/greetd.service.d/cloudplay.conf",
        "/etc/cloudplay/labwc/rc.xml", "/etc/cloudplay/labwc/autostart",
        "/usr/local/bin/cloudplay-start", "/usr/local/bin/cloudplay-session",
        "/usr/local/bin/cloudplay-browser-session", "/usr/local/lib/cloudplay/supervise.py",
        "/usr/share/plymouth/themes/cloudplay/cloudplay.script",
        "/usr/share/plymouth/themes/cloudplay/cloudplay.plymouth",
        "/boot/firmware/cmdline.txt", "/boot/firmware/config.txt",
    ]
    report = {
        "design": "lite-wayland-kiosk", "account": "cloudplay", "uid": 1000,
        "passwords_locked": True, "ssh_masked": True, "desktop_wizard_absent": True,
        "session": "greetd PAM/login + logind + dbus-run-session + labwc",
        "browser_release": "v0.4.1", "plymouth_theme": "cloudplay",
        "initramfs": initramfs, "boot_validated": False,
        "configuration_sha256": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
    }
    Path("/usr/local/share/cloudplay/kiosk-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
