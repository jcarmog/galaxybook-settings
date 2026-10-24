#!/usr/bin/env python3
"""galaxybookd - privileged D-Bus service exposing Samsung Galaxy Book controls.

Wraps the sysfs interfaces of the upstream `samsung-galaxybook` kernel driver
(platform profile, battery charge limit, keyboard backlight and the firmware
attributes usb_charging / power_on_lid_open / block_recording) behind a small
D-Bus API so unprivileged clients (the GTK app and the GNOME Shell extension)
can change them after a polkit check.

For development it can run on the session bus against a fake sysfs tree:
    galaxybookd.py --session --sysfs-root /tmp/fake-sysfs --no-polkit
"""

import argparse
import glob
import json
import logging
import os
import signal
import sys

from gi.repository import Gio, GLib

BUS_NAME = "io.github.galaxybook.Settings1"
OBJECT_PATH = "/io/github/galaxybook/Settings1"
IFACE = "io.github.galaxybook.Settings1"
POLKIT_ACTION = "io.github.galaxybook.settings.configure"
ERROR_PREFIX = "io.github.galaxybook.Error."
POLL_INTERVAL_MS = 2000

PPD_NAME = "org.freedesktop.UPower.PowerProfiles"
PPD_PATH = "/org/freedesktop/UPower/PowerProfiles"
# power-profiles-daemon names -> platform_profile choices that back them
PPD_PROFILE_FOR = {
    "low-power": "power-saver",
    "quiet": "power-saver",
    "cool": "power-saver",
    "balanced": "balanced",
    "performance": "performance",
}

FIRMWARE_ATTRIBUTES = {
    # D-Bus property name: sysfs attribute name
    "UsbCharging": "usb_charging",
    "PowerOnLidOpen": "power_on_lid_open",
    "BlockRecording": "block_recording",
}

INTROSPECTION_XML = f"""
<node>
  <interface name="{IFACE}">
    <property name="Features" type="as" access="read"/>
    <property name="PlatformProfile" type="s" access="read"/>
    <property name="PlatformProfileChoices" type="as" access="read"/>
    <property name="ChargeEndThreshold" type="u" access="read"/>
    <property name="KeyboardBacklight" type="u" access="read"/>
    <property name="KeyboardBacklightMax" type="u" access="read"/>
    <property name="UsbCharging" type="b" access="read"/>
    <property name="PowerOnLidOpen" type="b" access="read"/>
    <property name="BlockRecording" type="b" access="read"/>
    <method name="SetPlatformProfile"><arg name="profile" type="s" direction="in"/></method>
    <method name="SetChargeEndThreshold"><arg name="percent" type="u" direction="in"/></method>
    <method name="SetKeyboardBacklight"><arg name="level" type="u" direction="in"/></method>
    <method name="SetUsbCharging"><arg name="enabled" type="b" direction="in"/></method>
    <method name="SetPowerOnLidOpen"><arg name="enabled" type="b" direction="in"/></method>
    <method name="SetBlockRecording"><arg name="enabled" type="b" direction="in"/></method>
  </interface>
</node>
"""

PROPERTY_TYPES = {
    "Features": "as",
    "PlatformProfile": "s",
    "PlatformProfileChoices": "as",
    "ChargeEndThreshold": "u",
    "KeyboardBacklight": "u",
    "KeyboardBacklightMax": "u",
    "UsbCharging": "b",
    "PowerOnLidOpen": "b",
    "BlockRecording": "b",
}

log = logging.getLogger("galaxybookd")


class DBusError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = ERROR_PREFIX + name


class Hardware:
    """Thin read/write layer over the samsung-galaxybook sysfs files."""

    def __init__(self, root="/"):
        def p(path):
            return os.path.join(root, path.lstrip("/"))

        self.profile = p("/sys/firmware/acpi/platform_profile")
        self.profile_choices = p("/sys/firmware/acpi/platform_profile_choices")
        batteries = sorted(glob.glob(p("/sys/class/power_supply/BAT*/charge_control_end_threshold")))
        self.charge_threshold = batteries[0] if batteries else None
        leds = sorted(glob.glob(p("/sys/class/leds/samsung-galaxybook::kbd_backlight")))
        self.kbd = leds[0] if leds else None
        self.fw_attrs = p("/sys/class/firmware-attributes/samsung-galaxybook/attributes")

    @staticmethod
    def _read(path):
        with open(path) as f:
            return f.read().strip()

    @staticmethod
    def _write(path, value):
        with open(path, "w") as f:
            f.write(f"{value}\n")

    def _fw_path(self, attr):
        return os.path.join(self.fw_attrs, attr, "current_value")

    def features(self):
        feats = []
        if os.path.exists(self.profile):
            feats.append("platform-profile")
        if self.charge_threshold:
            feats.append("charge-threshold")
        if self.kbd:
            feats.append("kbd-backlight")
        for prop, attr in FIRMWARE_ATTRIBUTES.items():
            if os.path.exists(self._fw_path(attr)):
                feats.append(attr.replace("_", "-"))
        return feats

    def snapshot(self):
        """Current value of every property; missing features report defaults."""
        state = {
            "Features": self.features(),
            "PlatformProfile": "",
            "PlatformProfileChoices": [],
            "ChargeEndThreshold": 0,
            "KeyboardBacklight": 0,
            "KeyboardBacklightMax": 0,
        }
        try:
            state["PlatformProfile"] = self._read(self.profile)
            state["PlatformProfileChoices"] = self._read(self.profile_choices).split()
        except OSError:
            pass
        if self.charge_threshold:
            try:
                state["ChargeEndThreshold"] = int(self._read(self.charge_threshold))
            except (OSError, ValueError):
                pass
        if self.kbd:
            try:
                state["KeyboardBacklight"] = int(self._read(os.path.join(self.kbd, "brightness")))
                state["KeyboardBacklightMax"] = int(self._read(os.path.join(self.kbd, "max_brightness")))
            except (OSError, ValueError):
                pass
        for prop, attr in FIRMWARE_ATTRIBUTES.items():
            try:
                state[prop] = self._read(self._fw_path(attr)) == "1"
            except OSError:
                state[prop] = False
        return state

    def set_profile_sysfs(self, profile):
        self._require(os.path.exists(self.profile), "platform profile")
        choices = self._read(self.profile_choices).split()
        if profile not in choices:
            raise DBusError("InvalidArgs", f"Profile must be one of: {', '.join(choices)}")
        self._write(self.profile, profile)

    def set_charge_threshold(self, percent):
        self._require(self.charge_threshold, "battery charge limit")
        if not 1 <= percent <= 100:
            raise DBusError("InvalidArgs", "Charge limit must be between 1 and 100")
        self._write(self.charge_threshold, percent)

    def set_kbd_backlight(self, level):
        self._require(self.kbd, "keyboard backlight")
        maximum = int(self._read(os.path.join(self.kbd, "max_brightness")))
        if level > maximum:
            raise DBusError("InvalidArgs", f"Backlight level must be between 0 and {maximum}")
        self._write(os.path.join(self.kbd, "brightness"), level)

    def set_fw_attr(self, attr, enabled):
        path = self._fw_path(attr)
        self._require(os.path.exists(path), attr)
        self._write(path, 1 if enabled else 0)

    @staticmethod
    def _require(ok, what):
        if not ok:
            raise DBusError("NotSupported", f"This device does not support {what}")


class State:
    """Settings that the firmware may forget and we restore at startup."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not save state to %s: %s", self.path, e)


class Service:
    def __init__(self, bus, hw, state, use_polkit):
        self.bus = bus
        self.hw = hw
        self.state = state
        self.use_polkit = use_polkit
        self.cache = hw.snapshot()
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self.bus.register_object(OBJECT_PATH, node.interfaces[0], self._on_method_call, self._on_get_property, None)

    def restore(self):
        threshold = self.state.get("ChargeEndThreshold")
        if threshold and "charge-threshold" in self.cache["Features"] and threshold != self.cache["ChargeEndThreshold"]:
            try:
                self.hw.set_charge_threshold(threshold)
                log.info("Restored battery charge limit to %d%%", threshold)
            except (OSError, DBusError) as e:
                log.warning("Could not restore battery charge limit: %s", e)
        self.refresh()

    # -- properties ---------------------------------------------------------

    def _on_get_property(self, _bus, _sender, _path, _iface, name):
        return GLib.Variant(PROPERTY_TYPES[name], self.cache[name])

    def refresh(self):
        new = self.hw.snapshot()
        changed = {k: GLib.Variant(PROPERTY_TYPES[k], v) for k, v in new.items() if self.cache.get(k) != v}
        self.cache = new
        if changed:
            self.bus.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (IFACE, changed, [])),
            )
        return GLib.SOURCE_CONTINUE

    # -- methods ------------------------------------------------------------

    def _on_method_call(self, _bus, sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        self._authorize(sender, lambda: self._run(method, args, invocation), invocation)

    def _run(self, method, args, invocation):
        try:
            if method == "SetPlatformProfile":
                self._set_profile(args[0])
            elif method == "SetChargeEndThreshold":
                self.hw.set_charge_threshold(args[0])
                self.state.set("ChargeEndThreshold", args[0])
            elif method == "SetKeyboardBacklight":
                self.hw.set_kbd_backlight(args[0])
            elif method == "SetUsbCharging":
                self.hw.set_fw_attr("usb_charging", args[0])
            elif method == "SetPowerOnLidOpen":
                self.hw.set_fw_attr("power_on_lid_open", args[0])
            elif method == "SetBlockRecording":
                self.hw.set_fw_attr("block_recording", args[0])
            else:
                raise DBusError("UnknownMethod", method)
        except DBusError as e:
            invocation.return_dbus_error(e.name, str(e))
            return
        except OSError as e:
            log.error("%s failed: %s", method, e)
            invocation.return_dbus_error(ERROR_PREFIX + "Failed", str(e))
            return
        log.info("%s%r", method, tuple(args))
        self.refresh()
        invocation.return_value(None)

    def _set_profile(self, profile):
        """Go through power-profiles-daemon when it owns the platform profile,
        otherwise it would immediately overwrite a direct sysfs write."""
        ppd_profile = PPD_PROFILE_FOR.get(profile)
        if ppd_profile and self._ppd_running():
            choices = self.cache["PlatformProfileChoices"]
            if profile not in choices:
                raise DBusError("InvalidArgs", f"Profile must be one of: {', '.join(choices)}")
            try:
                self.bus.call_sync(
                    PPD_NAME, PPD_PATH, "org.freedesktop.DBus.Properties", "Set",
                    GLib.Variant("(ssv)", (PPD_NAME, "ActiveProfile", GLib.Variant("s", ppd_profile))),
                    None, Gio.DBusCallFlags.NONE, 5000, None,
                )
                return
            except GLib.Error as e:
                log.warning("power-profiles-daemon refused profile change, writing sysfs: %s", e.message)
        self.hw.set_profile_sysfs(profile)

    def _ppd_running(self):
        try:
            reply = self.bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                GLib.Variant("(s)", (PPD_NAME,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None,
            )
            return reply.unpack()[0]
        except GLib.Error:
            return False

    # -- polkit -------------------------------------------------------------

    def _authorize(self, sender, on_success, invocation):
        if not self.use_polkit:
            on_success()
            return
        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        allow_user_interaction = 1

        def done(bus, result):
            try:
                (authorized, _challenge, _details), = bus.call_finish(result).unpack()
            except GLib.Error as e:
                log.error("polkit check failed: %s", e.message)
                authorized = False
            if authorized:
                on_success()
            else:
                invocation.return_dbus_error(ERROR_PREFIX + "NotAuthorized", "Not authorized to change Galaxy Book settings")

        self.bus.call(
            "org.freedesktop.PolicyKit1",
            "/org/freedesktop/PolicyKit1/Authority",
            "org.freedesktop.PolicyKit1.Authority",
            "CheckAuthorization",
            GLib.Variant("((sa{sv})sa{ss}us)", (subject, POLKIT_ACTION, {}, allow_user_interaction, "")),
            GLib.VariantType("((bba{ss}))"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            done,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", action="store_true", help="use the session bus (development)")
    parser.add_argument("--sysfs-root", default="/", help="prefix for sysfs paths (development)")
    parser.add_argument("--state-file", default="/var/lib/galaxybook-settings/state.json")
    parser.add_argument("--no-polkit", action="store_true", help="skip authorization (development)")
    parser.add_argument("--debug", action="store_true")
    opts = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if opts.debug else logging.INFO, format="%(levelname)s: %(message)s")

    if not opts.session and os.geteuid() != 0:
        log.error("galaxybookd must run as root on the system bus (use --session for development)")
        return 1

    loop = GLib.MainLoop()
    bus_type = Gio.BusType.SESSION if opts.session else Gio.BusType.SYSTEM
    hw = Hardware(opts.sysfs_root)
    state = State(opts.state_file)

    def on_bus_acquired(bus, _name):
        service = Service(bus, hw, state, use_polkit=not opts.no_polkit)
        service.restore()
        GLib.timeout_add(POLL_INTERVAL_MS, service.refresh)
        log.info("Features: %s", ", ".join(service.cache["Features"]) or "none")

    def on_name_lost(_bus, _name):
        log.error("Could not own %s (already running?)", BUS_NAME)
        loop.quit()

    Gio.bus_own_name(bus_type, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus_acquired, None, on_name_lost)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
