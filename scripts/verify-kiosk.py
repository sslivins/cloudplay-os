#!/usr/bin/env python3
"""Fail the image export if the kiosk becomes a desktop or loses its boot wiring."""
import hashlib
import json
import configparser
import stat
import re
import subprocess
import tomllib
from pathlib import Path
from xml.etree import ElementTree


def effective_journal_settings(text):
    config = configparser.ConfigParser(strict=False, interpolation=None)
    config.optionxform = str
    config.read_string(text)
    return dict(config["Journal"])


def home_directory_metadata(home, uid, gid):
    metadata = {}
    for path in (home, home / ".config", home / ".config/cloudplay"):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode), path
        assert info.st_uid == uid and info.st_gid == gid, path
        assert stat.S_IMODE(info.st_mode) == 0o700, path
        metadata[str(path)] = {"uid": uid, "gid": gid, "mode": "0700"}
    return metadata


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
    required = {"greetd", "labwc", "libpam-systemd", "dbus-user-session",
                "pipewire", "pipewire-pulse", "wireplumber", "plymouth", "plymouth-themes",
                "dnsmasq-base", "python3-dbus", "python3-qrcode", "iw", "rfkill",
                "wpasupplicant", "wireless-regdb"}
    assert required <= names, f"Missing kiosk packages: {required - names}"
    account = pwd.getpwnam("cloudplay")
    assert account.pw_uid == 1000 and account.pw_shell == "/bin/bash"
    home_metadata = home_directory_metadata(Path(account.pw_dir), account.pw_uid, account.pw_gid)
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
    for unit in ("cloudplay-network.service", "cloudplay-wifi-radio.service"):
        assert Path("/etc/systemd/system/multi-user.target.wants", unit).is_symlink()
    cmdline = Path("/boot/firmware/cmdline.txt").read_text().split()
    assert {"quiet", "splash", "console=tty3", "vt.global_cursor_default=0"} <= set(cmdline)
    assert "console=tty1" not in cmdline
    assert "auto_initramfs=1" in Path("/boot/firmware/config.txt").read_text()
    assert subprocess.check_output(["plymouth-set-default-theme"], text=True).strip() == "cloudplay"
    artwork = Path("/usr/share/plymouth/themes/cloudplay/cloudplay.png")
    assert hashlib.sha256(artwork.read_bytes()).hexdigest() == "b2d7338996250ec4f9a34a8534d8bb9d04da7fa5e6305e431633fb184b57ae0b"
    initramfs = {}
    for path in sorted(Path("/boot").glob("initrd.img-*")):
        listing = subprocess.check_output(["lsinitramfs", str(path)], text=True)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.script" in listing, str(path)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.plymouth" in listing, str(path)
        assert "usr/share/plymouth/themes/cloudplay/cloudplay.png" in listing, str(path)
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
        "/usr/share/plymouth/themes/cloudplay/cloudplay.png",
        "/boot/firmware/cmdline.txt", "/boot/firmware/config.txt",
        "/etc/systemd/system/cloudplay-network.service",
        "/etc/systemd/system/cloudplay-wifi-radio.service",
        "/etc/systemd/journald.conf.d/cloudplay-diagnostics.conf",
    ]
    paths += ["/usr/local/lib/cloudplay/onboarding/" + name for name in
              ("network.py", "service.py", "client.py", "setup.html", "setup.js", "setup.css")]
    for path in paths:
        info = Path(path).stat()
        assert info.st_uid == 0 and not info.st_mode & 0o022, path
    report = {
        "design": "lite-wayland-kiosk", "account": "cloudplay", "uid": 1000,
        "passwords_locked": True, "ssh_masked": True, "desktop_wizard_absent": True,
        "session": "greetd PAM/login + logind + dbus-run-session + labwc",
        "browser_release": "v0.4.1", "plymouth_theme": "cloudplay",
        "onboarding": "network-only; separate sandboxed setup browser; private optional phone AP",
        "journal_effective_settings": journal,
        "home_directories": home_metadata,
        "initramfs": initramfs, "boot_validated": False,
        "configuration_sha256": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
    }
    Path("/usr/local/share/cloudplay/kiosk-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
