"""Agora phase-one network patterns, adapted to secret-safe NetworkManager D-Bus."""
import ipaddress
import subprocess
import time
import uuid
from pathlib import Path

NM = "org.freedesktop.NetworkManager"
ROOT = "/org/freedesktop/NetworkManager"
AP_ADDRESS = "10.42.0.1"


def countries(path=Path("/usr/share/zoneinfo/iso3166.tab")):
    return {line.split("\t")[0]: line.split("\t")[1]
            for line in path.read_text().splitlines() if line and not line.startswith("#")}


def validate_wifi(data, allowed_countries):
    if not isinstance(data, dict):
        raise ValueError("Invalid request")
    ssid, password = data.get("ssid", ""), data.get("password", "")
    security, country = data.get("security", ""), data.get("country", "")
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32 or "\0" in ssid:
        raise ValueError("SSID must be 1–32 UTF-8 bytes")
    if not isinstance(password, str) or "\0" in password:
        raise ValueError("Invalid Wi-Fi password")
    if not isinstance(security, str) or security not in ("open", "wpa-psk", "sae", "owe"):
        raise ValueError("Select a supported Wi-Fi security mode")
    if security == "wpa-psk" and not (
        8 <= len(password.encode()) <= 63
        or len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password)
    ):
        raise ValueError("WPA password must be 8–63 bytes or a 64-digit hex key")
    if security == "sae" and not 1 <= len(password.encode()) <= 63:
        raise ValueError("WPA3 password must be 1–63 bytes")
    if security in ("open", "owe") and password:
        raise ValueError("This network does not use a password")
    if not isinstance(country, str) or country not in allowed_countries:
        raise ValueError("Select your actual regulatory country")
    hidden = data.get("hidden", False)
    if not isinstance(hidden, bool):
        raise ValueError("Invalid hidden-network option")
    return dict(ssid=ssid, password=password, security=security, country=country, hidden=hidden)


def security_mode(flags, wpa, rsn):
    security = wpa | rsn
    if security & 0x100:
        return "wpa-psk"
    if security & 0x400:
        return "sae"
    if security & 0x800:
        return "owe"
    return "unsupported" if flags & 1 or security else "open"


def usable_address(address):
    try:
        value = ipaddress.ip_address(address)
        return not (value.is_loopback or value.is_link_local or value.is_unspecified)
    except ValueError:
        return False


class Network:
    def __init__(self):
        import dbus
        self.dbus = dbus
        self.bus = dbus.SystemBus(private=True)
        self.ap_connection = None

    def call(self, path, interface, method, *args):
        obj = self.bus.get_object(NM, path)
        return getattr(self.dbus.Interface(obj, interface), method)(*args, timeout=10)

    def properties(self, path, interface):
        return self.call(path, "org.freedesktop.DBus.Properties", "GetAll", interface)

    def devices(self):
        return [(str(path), self.properties(path, NM + ".Device"))
                for path in self.call(ROOT, NM, "GetDevices")]

    def wifi(self):
        return next(((path, props) for path, props in self.devices()
                     if props["DeviceType"] == 2), None)

    def device_ready(self, path, props):
        if props["State"] != 100:
            return False
        if props["DeviceType"] == 2:
            if self.properties(path, NM + ".Device.Wireless")["Mode"] != 2:
                return False  # Our hotspot having an address is NOT successful provisioning.
        for version in (4, 6):
            config = str(props.get(f"Ip{version}Config", "/"))
            if config != "/":
                data = self.properties(config, NM + f".IP{version}Config")
                if any(usable_address(str(item["address"])) for item in data.get("AddressData", [])):
                    return True
        return False

    def snapshot(self):
        devices = self.devices()
        return {
            "connected": any(self.device_ready(path, props) for path, props in devices
                             if props["DeviceType"] in (1, 2)),
            "has_wifi": any(props["DeviceType"] == 2 for _, props in devices),
        }

    def enable(self):
        current = self.properties(ROOT, NM)
        if not current["NetworkingEnabled"]:
            try:
                self.call(ROOT, NM, "Enable", True)
            except self.dbus.DBusException as error:
                # Another client may have enabled it after our read. NM rejects no-op Enable calls.
                if (error.get_dbus_name() != NM + ".AlreadyEnabledOrDisabled"
                        or not self.properties(ROOT, NM)["NetworkingEnabled"]):
                    raise
        if not current["WirelessEnabled"]:
            self.call(ROOT, "org.freedesktop.DBus.Properties", "Set",
                      NM, "WirelessEnabled", self.dbus.Boolean(True))

    def scan(self):
        wifi = self.wifi()
        if not wifi:
            return []
        device, _ = wifi
        try:
            self.call(device, NM + ".Device.Wireless", "RequestScan",
                      self.dbus.Dictionary({}, signature="sv"))
        except self.dbus.DBusException:
            pass  # Cached results remain useful if a driver is busy or scan-throttled.
        found = {}
        for path in self.call(device, NM + ".Device.Wireless", "GetAllAccessPoints"):
            props = self.properties(path, NM + ".AccessPoint")
            raw = bytes(props["Ssid"])
            try:
                ssid = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not ssid or "\0" in ssid:
                continue
            security = security_mode(int(props["Flags"]), int(props["WpaFlags"]), int(props["RsnFlags"]))
            item = dict(ssid=ssid, signal=int(props["Strength"]), security=security)
            key = (ssid, security)
            if key not in found or item["signal"] > found[key]["signal"]:
                found[key] = item
        return sorted(found.values(), key=lambda item: item["signal"], reverse=True)

    def set_country(self, country):
        subprocess.run(["/usr/sbin/iw", "reg", "set", country], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

    def activate(self, data, hotspot=False):
        wifi = self.wifi()
        if not wifi:
            raise RuntimeError("No Wi-Fi interface")
        device, props = wifi
        d = self.dbus
        settings = {
            "connection": {
                "id": "Cloudplay hotspot" if hotspot else "Cloudplay Wi-Fi",
                "uuid": str(uuid.uuid4()), "type": "802-11-wireless",
                "interface-name": str(props["Interface"]),
                "autoconnect": d.Boolean(not hotspot),
                "autoconnect-priority": d.Int32(10),
            },
            "802-11-wireless": {
                "ssid": d.ByteArray(data["ssid"].encode()),
                "mode": "ap" if hotspot else "infrastructure",
                "hidden": d.Boolean(data.get("hidden", False)),
            },
            "ipv4": {"method": "shared" if hotspot else "auto"},
            "ipv6": {"method": "disabled" if hotspot else "auto"},
        }
        if hotspot:
            settings["802-11-wireless"].update(band="bg", channel=d.UInt32(6))
            settings["ipv4"]["address-data"] = d.Array([
                d.Dictionary({"address": AP_ADDRESS, "prefix": d.UInt32(24)}, signature="sv")
            ], signature="a{sv}")
        if data["security"] != "open":
            settings["802-11-wireless"]["security"] = "802-11-wireless-security"
            settings["802-11-wireless-security"] = {"key-mgmt": data["security"]}
            if data["security"] in ("wpa-psk", "sae"):
                settings["802-11-wireless-security"]["psk"] = data["password"]
        options = {"persist": "volatile" if hotspot else "memory",
                   "bind-activation": "dbus-client" if hotspot else "none"}
        typed = d.Dictionary({key: d.Dictionary(value, signature="sv")
                              for key, value in settings.items()}, signature="sa{sv}")
        connection = None
        try:
            connection, active, _ = self.call(
                ROOT, NM, "AddAndActivateConnection2", typed, d.ObjectPath(device),
                d.ObjectPath("/"), d.Dictionary(options, signature="sv"))
            if hotspot:
                self.ap_connection = str(connection)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                state = int(self.properties(active, NM + ".Connection.Active")["State"])
                if state == 2:
                    current = self.properties(device, NM + ".Device")
                    if hotspot or self.device_ready(device, current):
                        if not hotspot:
                            self.call(connection, NM + ".Settings.Connection", "Save")
                        return
                if state == 4:
                    break
                time.sleep(0.5)
            raise RuntimeError("Connection did not obtain an address")
        except Exception:
            # Never delete a previous saved network just because a new attempt failed.
            try:
                if connection is None:
                    connection = self.call(ROOT + "/Settings", NM + ".Settings",
                                           "GetConnectionByUuid", settings["connection"]["uuid"])
                self.call(connection, NM + ".Settings.Connection", "Delete")
            except Exception:
                pass
            if hotspot:
                self.ap_connection = None
            raise

    def stop_ap(self):
        if self.ap_connection:
            try:
                self.call(self.ap_connection, NM + ".Settings.Connection", "Delete")
            finally:
                self.ap_connection = None
