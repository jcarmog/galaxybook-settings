#!/usr/bin/env python3
"""galaxyphoned - per-user D-Bus service for a paired Samsung Galaxy phone.

Works over Bluetooth with nothing set up on the phone, and does more when adb
can reach it (USB debugging, or wireless debugging paired once with AdbPair):

    Bluetooth   battery in 20% steps (PipeWire reports the phone's handsfree
                battery indicator to BlueZ), SMS notifications through MAP,
                sending files through OBEX Object Push
    adb         exact battery and charging state, notifications from every
                app, sending files with adb push, screen mirroring with scrcpy

Status goes on the session bus for the GTK app and the GNOME Shell extension;
new SMS and phone notifications are posted as desktop notifications.
"""

import argparse
import logging
import os
import re
import shutil
import signal
import sys

from gi.repository import Gio, GLib

BUS_NAME = "io.github.jcarmog.GalaxyBook.Phone1"
OBJECT_PATH = "/io/github/jcarmog/GalaxyBook/Phone1"
IFACE = "io.github.jcarmog.GalaxyBook.Phone1"
ERROR_PREFIX = "io.github.jcarmog.GalaxyBook.Error."
DESKTOP_ENTRY = "io.github.jcarmog.GalaxyBook.Settings"

BLUEZ = "org.bluez"
OBEX = "org.bluez.obex"
OBEX_PATH = "/org/bluez/obex"

ADB_RESTART_S = 10
ADB_BATTERY_S = 60
ADB_NOTIFICATIONS_S = 15
MAP_POLL_S = 120
MAP_TIMEOUT_MS = 60000
PHONE_DOWNLOADS = "/sdcard/Download/"

# Notification flags (android.app.Notification) we don't forward
FLAG_ONGOING_EVENT = 0x2
FLAG_FOREGROUND_SERVICE = 0x40
FLAG_GROUP_SUMMARY = 0x200
SKIP_FLAGS = FLAG_ONGOING_EVENT | FLAG_FOREGROUND_SERVICE | FLAG_GROUP_SUMMARY
# Android 17 prints flags by name ("ONGOING_EVENT|CAN_COLORIZE"); other names don't matter here
FLAG_NAMES = {"ONGOING_EVENT": FLAG_ONGOING_EVENT, "FOREGROUND_SERVICE": FLAG_FOREGROUND_SERVICE,
              "GROUP_SUMMARY": FLAG_GROUP_SUMMARY}
SKIP_PACKAGES = {"android", "com.android.systemui", "com.android.shell", "com.android.providers.downloads"}
BATTERY_STATUS_CHARGING = 2

APP_LABELS = {
    "com.samsung.android.messaging": "Messages",
    "com.google.android.apps.messaging": "Messages",
    "com.samsung.android.dialer": "Phone",
    "com.whatsapp": "WhatsApp",
    "com.whatsapp.w4b": "WhatsApp Business",
    "org.telegram.messenger": "Telegram",
    "com.google.android.gm": "Gmail",
    "com.microsoft.teams": "Teams",
    "com.instagram.android": "Instagram",
}

INTROSPECTION_XML = f"""
<node>
  <interface name="{IFACE}">
    <property name="Connected" type="b" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Address" type="s" access="read"/>
    <property name="Battery" type="i" access="read"/>
    <property name="BatteryCharging" type="b" access="read"/>
    <property name="BatterySource" type="s" access="read"/>
    <property name="AdbConnected" type="b" access="read"/>
    <property name="AdbSerial" type="s" access="read"/>
    <property name="CanMirror" type="b" access="read"/>
    <method name="SendFiles"><arg name="paths" type="as" direction="in"/></method>
    <method name="StartMirroring"/>
    <signal name="RaiseMirroring"><arg name="pids" type="au"/></signal>
    <method name="AdbPair">
      <arg name="address" type="s" direction="in"/>
      <arg name="code" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""

PROPERTY_TYPES = {
    "Connected": "b",
    "Name": "s",
    "Address": "s",
    "Battery": "i",
    "BatteryCharging": "b",
    "BatterySource": "s",
    "AdbConnected": "b",
    "AdbSerial": "s",
    "CanMirror": "b",
}

INITIAL_STATE = {
    "Connected": False,
    "Name": "",
    "Address": "",
    "Battery": -1,
    "BatteryCharging": False,
    "BatterySource": "",
    "AdbConnected": False,
    "AdbSerial": "",
    "CanMirror": False,
}

log = logging.getLogger("galaxyphoned")


class DBusError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = ERROR_PREFIX + name


# -- parsing ----------------------------------------------------------------


class TrackDevicesReader:
    """Splits `adb track-devices` output into device lists. Each block is a 4-digit hex
    length followed by that many bytes of "serial<TAB>state" lines, and lists every device."""

    def __init__(self):
        self.buffer = b""

    def feed(self, data):
        self.buffer += data
        lists = []
        while len(self.buffer) >= 4:
            try:
                size = int(self.buffer[:4], 16)
            except ValueError:
                self.buffer = b""
                break
            if len(self.buffer) < 4 + size:
                break
            block, self.buffer = self.buffer[4:4 + size], self.buffer[4 + size:]
            devices = []
            for line in block.decode(errors="replace").splitlines():
                serial, _, state = line.partition("\t")
                if serial:
                    devices.append((serial, state.split()[0] if state else ""))
            lists.append(devices)
        return lists


def pick_adb_device(devices):
    """The serial of the first usable (authorized, non-emulator) device, else None."""
    for serial, state in devices:
        if state == "device" and not serial.startswith("emulator-"):
            return serial
    return None


def parse_battery(text):
    """(level, charging) from `dumpsys battery`, or None if there is no level."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        level = int(fields["level"])
        scale = int(fields.get("scale", "100")) or 100
    except (KeyError, ValueError):
        return None
    status = fields.get("status", "")
    return round(100 * level / scale), status == str(BATTERY_STATUS_CHARGING)


RECORD_RE = re.compile(r"NotificationRecord\(0x[0-9a-f]+: pkg=(\S+) .*?key=(\S+): Notification\(")
EXTRA_RE = re.compile(r"^\s*android\.(title|text|bigText)=\w+ \((.*)\)\s*$")
FLAGS_RE = re.compile(r"^\s*flags=(\S+)\s*$")


def parse_flags(value):
    """Notification flags written as hex (Android 14-16) or as names joined by | (Android 17)."""
    if value.lower().startswith("0x"):
        return int(value, 16)
    if value.isdigit():
        return int(value)
    return sum(FLAG_NAMES.get(name, 0) for name in set(value.split("|")))


def parse_notifications(text):
    """Active notifications from `dumpsys notification --noredact`, as dicts with
    key, pkg, flags, title and text. Records listed twice keep the first entry."""
    records = {}
    current = None
    for line in text.splitlines():
        match = RECORD_RE.search(line)
        if match:
            pkg, key = match.groups()
            current = None if key in records else {"key": key, "pkg": pkg, "flags": 0, "title": "", "text": ""}
            if current:
                records[key] = current
            continue
        if current is None:
            continue
        match = FLAGS_RE.match(line)
        if match and not current.get("_flags_seen"):
            current["flags"] = parse_flags(match.group(1))
            current["_flags_seen"] = True
            continue
        match = EXTRA_RE.match(line)
        if match:
            field, value = match.groups()
            if value == "null":
                continue
            if field == "bigText":
                current["text"] = value  # the full text of an expanded notification
            elif not current[field]:
                current[field] = value
    for record in records.values():
        record.pop("_flags_seen", None)
    return list(records.values())


def forwardable(record):
    return (record["pkg"] not in SKIP_PACKAGES and not record["flags"] & SKIP_FLAGS
            and bool(record["title"] or record["text"]))


def app_label(pkg):
    if pkg in APP_LABELS:
        return APP_LABELS[pkg]
    return pkg.rsplit(".", 1)[-1].replace("_", " ").capitalize()


class NotificationTracker:
    """Remembers which phone notifications have been shown. The first snapshot only
    primes it, so notifications already on the phone are not replayed."""

    def __init__(self):
        self.seen = None

    def new(self, records):
        current = {(r["key"], r["title"], r["text"]): r for r in records if forwardable(r)}
        fresh = [] if self.seen is None else [r for ident, r in current.items() if ident not in self.seen]
        self.seen = set(current)
        return fresh

    def reset(self):
        self.seen = None


def is_phone(props):
    return props.get("Paired", False) and props.get("Icon") == "phone"


# -- desktop notifications --------------------------------------------------


class Notifier:
    def __init__(self, bus):
        self.bus = bus

    def show(self, summary, body="", app_name="Galaxy phone", icon="phone-symbolic"):
        hints = {"desktop-entry": GLib.Variant("s", DESKTOP_ENTRY)}
        self.bus.call(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
            "Notify", GLib.Variant("(susssasa{sv}i)", (app_name, 0, icon, summary, body, [], hints, -1)),
            None, Gio.DBusCallFlags.NONE, -1, None, self._done)

    @staticmethod
    def _done(bus, result):
        try:
            bus.call_finish(result)
        except GLib.Error as e:
            log.warning("Could not show notification: %s", e.message)


def run_async(argv, callback):
    """Run a command and call callback(ok, stdout, stderr) when it exits."""
    try:
        proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as e:
        callback(False, "", e.message)
        return

    def done(proc, result):
        try:
            _ok, out, err = proc.communicate_utf8_finish(result)
        except GLib.Error as e:
            callback(False, "", e.message)
            return
        callback(proc.get_successful(), out or "", err or "")

    proc.communicate_utf8_async(None, None, done)


def scrcpy_pids(proc_dir="/proc"):
    """PIDs of this user's running scrcpy processes (started by us or from a terminal)."""
    uid = os.getuid()
    pids = []
    for pid in os.listdir(proc_dir):
        if not pid.isdigit():
            continue
        try:
            if os.stat(os.path.join(proc_dir, pid)).st_uid != uid:
                continue
            with open(os.path.join(proc_dir, pid, "cmdline"), "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0]
        except OSError:
            continue
        if os.path.basename(argv0) == b"scrcpy":
            pids.append(int(pid))
    return sorted(pids)


# -- D-Bus service ----------------------------------------------------------


class Service:
    """Session-bus object combining what Bluetooth and adb know about the phone."""

    def __init__(self, bus, notifier, adb_path, scrcpy_path):
        self.bus = bus
        self.notifier = notifier
        self.adb_path = adb_path
        self.scrcpy_path = scrcpy_path
        self.cache = dict(INITIAL_STATE)
        self.bt_battery = -1
        self.adb_battery = None  # (level, charging) from the last dumpsys
        self.bluez = None
        self.adb = None
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self.bus.register_object(OBJECT_PATH, node.interfaces[0], self._on_method_call, self._on_get_property, None)

    # -- properties ---------------------------------------------------------

    def _on_get_property(self, _bus, _sender, _path, _iface, name):
        return GLib.Variant(PROPERTY_TYPES[name], self.cache[name])

    def update(self, values):
        changed = {k: GLib.Variant(PROPERTY_TYPES[k], v) for k, v in values.items() if self.cache.get(k) != v}
        self.cache.update(values)
        if changed:
            self.bus.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (IFACE, changed, [])),
            )

    def set_bt_battery(self, level):
        self.bt_battery = level
        self._update_battery()

    def set_adb_battery(self, battery):
        self.adb_battery = battery
        self._update_battery()

    def _update_battery(self):
        if self.adb_battery:
            level, charging = self.adb_battery
            self.update({"Battery": level, "BatteryCharging": charging, "BatterySource": "adb"})
        elif self.bt_battery >= 0 and self.cache["Connected"]:
            self.update({"Battery": self.bt_battery, "BatteryCharging": False, "BatterySource": "bluetooth"})
        else:
            self.update({"Battery": -1, "BatteryCharging": False, "BatterySource": ""})

    def set_adb_serial(self, serial):
        if not serial:
            self.adb_battery = None
        self.update({"AdbConnected": bool(serial), "AdbSerial": serial or "",
                     "CanMirror": bool(serial and self.scrcpy_path)})
        self._update_battery()

    # -- methods ------------------------------------------------------------

    def _on_method_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        try:
            if method == "SendFiles":
                self.send_files(list(args[0]))
            elif method == "StartMirroring":
                self.start_mirroring()
            elif method == "AdbPair":
                self.adb_pair(*args, invocation)
                return  # replies when adb exits
            else:
                raise DBusError("UnknownMethod", method)
        except DBusError as e:
            # The caller shows the error; log it too so failed calls show up in the journal
            log.warning("%s failed: %s", method, e)
            invocation.return_dbus_error(e.name, str(e))
            return
        log.info("%s%r", method, args if method != "AdbPair" else ())
        invocation.return_value(None)

    def send_files(self, paths):
        if not paths:
            raise DBusError("InvalidArgs", "No files given")
        for path in paths:
            if not os.path.isabs(path) or not os.path.isfile(path):
                raise DBusError("InvalidArgs", f"Not a file: {path}")
        what = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)} files"
        if self.cache["AdbConnected"]:
            argv = [self.adb_path, "-s", self.cache["AdbSerial"], "push", *paths, PHONE_DOWNLOADS]

            def done(ok, _out, err):
                if ok:
                    self.notifier.show(f"Sent {what} to your phone", "Saved in Downloads")
                else:
                    log.warning("adb push failed: %s", err.strip())
                    self.notifier.show(f"Could not send {what}", err.strip().splitlines()[-1] if err.strip() else "")

            run_async(argv, done)
        elif self.cache["Connected"] and self.bluez:
            self.bluez.push_files(paths, what)
        else:
            raise DBusError("NotConnected", "The phone is not connected")

    def start_mirroring(self):
        if not self.cache["AdbConnected"]:
            raise DBusError("NotConnected", "Screen mirroring needs adb: turn on USB or wireless debugging on the phone")
        if not self.scrcpy_path:
            raise DBusError("NotSupported", "scrcpy is not installed")
        pids = scrcpy_pids()
        if pids:
            # Only the shell may raise a window on Wayland: the extension listens for this
            log.info("Screen mirroring is already running; asking the shell to raise it")
            self.bus.emit_signal(None, OBJECT_PATH, IFACE, "RaiseMirroring", GLib.Variant("(au)", (pids,)))
            return
        title = self.cache["Name"] or "Galaxy phone"
        try:
            Gio.Subprocess.new([self.scrcpy_path, "-s", self.cache["AdbSerial"], "--window-title", title],
                               Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            raise DBusError("Failed", f"Could not start scrcpy: {e.message}") from e

    def adb_pair(self, address, code, invocation):
        if not self.adb_path:
            raise DBusError("NotSupported", "adb is not installed")
        if not re.fullmatch(r"[\w.:\[\]-]+:\d+", address) or not re.fullmatch(r"\d{6}", code):
            raise DBusError("InvalidArgs", "Enter the IP address:port and the 6-digit code shown on the phone")

        def done(ok, out, err):
            text = (out + err).strip()
            if ok and "Successfully paired" in text:
                log.info("Paired with %s over wireless debugging", address)
                invocation.return_value(None)
            else:
                log.warning("adb pair failed: %s", text)
                invocation.return_dbus_error(ERROR_PREFIX + "Failed", text.splitlines()[-1] if text else "Pairing failed")

        run_async([self.adb_path, "pair", address, code], done)


# -- Bluetooth --------------------------------------------------------------


class BluezWatcher:
    """Follows the paired phone in BlueZ (connection, battery), keeps a MAP session open
    for SMS notifications while it is connected, and pushes files over OBEX."""

    def __init__(self, system_bus, session_bus, service):
        self.bus = system_bus
        self.session = session_bus
        self.service = service
        self.phones = {}  # device path -> Device1 properties
        self.batteries = {}  # device path -> percentage
        self.path = None
        self.map_session = None
        self.map_pending = False
        self.map_seen = None
        self.map_refresh_id = 0
        self.map_poll_id = 0
        self.transfers = {}  # transfer path -> push job dict
        service.bluez = self

        self.bus.signal_subscribe(BLUEZ, "org.freedesktop.DBus.Properties", "PropertiesChanged", None, None,
                                  Gio.DBusSignalFlags.NONE, self._on_props_changed)
        self.bus.signal_subscribe(BLUEZ, "org.freedesktop.DBus.ObjectManager", None, "/", None,
                                  Gio.DBusSignalFlags.NONE, self._on_objects_changed)
        self.session.signal_subscribe(OBEX, "org.freedesktop.DBus.ObjectManager", "InterfacesAdded", "/", None,
                                      Gio.DBusSignalFlags.NONE, self._on_obex_added)
        self.session.signal_subscribe(OBEX, "org.freedesktop.DBus.Properties", "PropertiesChanged", None,
                                      "org.bluez.obex.Transfer1", Gio.DBusSignalFlags.NONE, self._on_transfer_changed)

        reply = self.bus.call_sync(
            BLUEZ, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
            None, GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, -1, None)
        for path, ifaces in reply.unpack()[0].items():
            self._add_object(path, ifaces)
        self._choose()

    def _add_object(self, path, ifaces):
        if "org.bluez.Device1" in ifaces and is_phone(ifaces["org.bluez.Device1"]):
            self.phones[path] = dict(ifaces["org.bluez.Device1"])
        if "org.bluez.Battery1" in ifaces:
            self.batteries[path] = ifaces["org.bluez.Battery1"].get("Percentage", -1)

    def _on_objects_changed(self, _bus, _sender, _path, _iface, signal_name, params):
        if signal_name == "InterfacesAdded":
            path, ifaces = params.unpack()
            self._add_object(path, ifaces)
        elif signal_name == "InterfacesRemoved":
            path, names = params.unpack()
            if "org.bluez.Device1" in names:
                self.phones.pop(path, None)
            if "org.bluez.Battery1" in names:
                self.batteries.pop(path, None)
        else:
            return
        self._choose()

    def _on_props_changed(self, _bus, _sender, path, _iface, _signal, params):
        iface, changed, _invalidated = params.unpack()
        if iface == "org.bluez.Battery1" and "Percentage" in changed:
            self.batteries[path] = changed["Percentage"]
        elif iface == "org.bluez.Device1":
            if path in self.phones:
                self.phones[path].update(changed)
            elif "Paired" in changed or "Icon" in changed:
                try:
                    props = self._device_props(path)
                except GLib.Error:
                    return
                if is_phone(props):
                    self.phones[path] = props
            else:
                return
        else:
            return
        self._choose()

    def _device_props(self, path):
        reply = self.bus.call_sync(
            BLUEZ, path, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", ("org.bluez.Device1",)),
            GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, -1, None)
        return reply.unpack()[0]

    def _choose(self):
        """Show the connected phone, else the first paired one."""
        if not self.phones:
            path = None
        else:
            path = next((p for p, props in self.phones.items() if props.get("Connected")), sorted(self.phones)[0])
        props = self.phones.get(path, {})
        connected = bool(props.get("Connected"))
        was_connected = self.service.cache["Connected"] and path == self.path
        self.path = path
        self.service.update({"Connected": connected, "Name": props.get("Alias") or props.get("Name", ""),
                             "Address": props.get("Address", "")})
        self.service.set_bt_battery(self.batteries.get(path, -1) if connected else -1)
        if connected and not was_connected:
            log.info("Phone connected: %s (%s)", self.service.cache["Name"], self.service.cache["Address"])
            self._open_map()
        elif not connected and was_connected:
            log.info("Phone disconnected")
            self._close_map()

    # -- SMS (MAP) ----------------------------------------------------------

    def _obex_call(self, path, iface, method, args, callback, timeout=30000):
        def done(bus, result):
            try:
                reply = bus.call_finish(result)
            except GLib.Error as e:
                callback(None, e)
                return
            callback(reply.unpack(), None)

        self.session.call(OBEX, path, iface, method, args, None, Gio.DBusCallFlags.NONE, timeout, None, done)

    def _open_map(self):
        if self.map_session or self.map_pending:
            return
        self.map_pending = True
        address = self.service.cache["Address"]

        def created(reply, error):
            self.map_pending = False
            if error:
                log.warning("No SMS access (MAP): %s. Allow message access for this computer on the phone.",
                            error.message)
                return
            if not self.service.cache["Connected"]:
                self._remove_session(reply[0])
                return
            self.map_session = reply[0]
            self.map_seen = None
            log.info("SMS access open (%s)", self.map_session)
            self._obex_call(self.map_session, "org.bluez.obex.MessageAccess1", "SetFolder",
                            GLib.Variant("(s)", ("/telecom/msg",)), folder_set)

        def folder_set(_reply, error):
            if error:
                log.warning("Could not open the SMS folder: %s", error.message)
                return
            self._refresh_inbox()
            self.map_poll_id = GLib.timeout_add_seconds(MAP_POLL_S, self._poll_inbox)

        self._obex_call(OBEX_PATH, "org.bluez.obex.Client1", "CreateSession",
                        GLib.Variant("(sa{sv})", (address, {"Target": GLib.Variant("s", "map")})),
                        created, MAP_TIMEOUT_MS)

    def _remove_session(self, path):
        self._obex_call(OBEX_PATH, "org.bluez.obex.Client1", "RemoveSession", GLib.Variant("(o)", (path,)),
                        lambda _r, _e: None)

    def _close_map(self):
        for source in ("map_poll_id", "map_refresh_id"):
            if getattr(self, source):
                GLib.source_remove(getattr(self, source))
                setattr(self, source, 0)
        if self.map_session:
            self._remove_session(self.map_session)
            self.map_session = None

    def _poll_inbox(self):
        self._refresh_inbox()
        return GLib.SOURCE_CONTINUE

    def _on_obex_added(self, _bus, _sender, _path, _iface, _signal, params):
        path, ifaces = params.unpack()
        # obexd turns MAP "new message" events into Message1 objects under the session
        if (self.map_session and path.startswith(self.map_session + "/") and "org.bluez.obex.Message1" in ifaces
                and not self.map_refresh_id):
            self.map_refresh_id = GLib.timeout_add(1500, self._refresh_inbox)

    def _refresh_inbox(self):
        self.map_refresh_id = 0
        if not self.map_session:
            return GLib.SOURCE_REMOVE
        session = self.map_session
        filters = {"MaxCount": GLib.Variant("q", 20), "Read": GLib.Variant("b", False)}

        def listed(reply, error):
            if error:
                log.warning("Could not list SMS: %s", error.message)
                return
            if session != self.map_session:
                return
            unread = reply[0]
            if self.map_seen is not None and not self.service.cache["AdbConnected"]:
                # With adb up, the Messages app notification arrives through adb instead
                for path in sorted(set(unread) - self.map_seen, key=lambda p: unread[p].get("Timestamp", "")):
                    msg = unread[path]
                    if msg.get("Type", "").startswith(("sms", "mms")):
                        sender = msg.get("Sender") or msg.get("SenderAddress") or "Unknown sender"
                        log.info("Relayed an SMS over Bluetooth (MAP)")
                        self.service.notifier.show(sender, msg.get("Subject") or msg.get("Text", ""),
                                                   app_name="Messages", icon="mail-unread-symbolic")
            self.map_seen = (self.map_seen or set()) | set(unread)

        self._obex_call(session, "org.bluez.obex.MessageAccess1", "ListMessages",
                        GLib.Variant("(sa{sv})", ("inbox", filters)), listed)
        return GLib.SOURCE_REMOVE

    # -- files (OPP) --------------------------------------------------------

    def push_files(self, paths, what):
        job = {"what": what, "left": len(paths), "failed": False, "session": None}

        def created(reply, error):
            if error:
                log.warning("OBEX push session failed: %s", error.message)
                self.service.notifier.show(f"Could not send {what}", error.message)
                return
            job["session"] = reply[0]
            for path in paths:
                self._obex_call(job["session"], "org.bluez.obex.ObjectPush1", "SendFile",
                                GLib.Variant("(s)", (path,)), lambda r, e: queued(r, e))

        def queued(reply, error):
            if error:
                log.warning("OBEX SendFile failed: %s", error.message)
                job["failed"] = True
                self._transfer_done(job)
            else:
                self.transfers[reply[0]] = job

        self.service.notifier.show(f"Sending {what}", "Accept the transfer on your phone")
        self._obex_call(OBEX_PATH, "org.bluez.obex.Client1", "CreateSession",
                        GLib.Variant("(sa{sv})", (self.service.cache["Address"], {"Target": GLib.Variant("s", "opp")})),
                        created, MAP_TIMEOUT_MS)

    def _on_transfer_changed(self, _bus, _sender, path, _iface, _signal, params):
        _iface_name, changed, _invalidated = params.unpack()
        status = changed.get("Status")
        if path in self.transfers and status in ("complete", "error"):
            job = self.transfers.pop(path)
            job["failed"] |= status == "error"
            self._transfer_done(job)

    def _transfer_done(self, job):
        job["left"] -= 1
        if job["left"] > 0:
            return
        if job["session"]:
            self._remove_session(job["session"])
        if job["failed"]:
            log.warning("Bluetooth transfer of %s failed or was declined", job["what"])
            self.service.notifier.show(f"Could not send {job['what']}", "The phone declined or the transfer failed")
        else:
            log.info("Sent %s over Bluetooth", job["what"])
            self.service.notifier.show(f"Sent {job['what']} to your phone")

    def close(self):
        self._close_map()


# -- adb --------------------------------------------------------------------


def find_tool(name, env_var, extra=()):
    candidates = [os.environ.get(env_var), shutil.which(name), *extra]
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


def find_adb():
    sdk_dirs = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                os.path.expanduser("~/Android/Sdk")]
    return find_tool("adb", "ADB", [os.path.join(d, "platform-tools", "adb") for d in sdk_dirs if d])


class AdbWatcher:
    """Tracks adb devices and, while one is attached, polls its battery and notifications."""

    def __init__(self, adb_path, service):
        self.adb = adb_path
        self.service = service
        self.proc = None
        self.reader = None
        self.serial = None
        self.timers = []
        self.tracker = NotificationTracker()
        self.closing = False
        service.adb = self
        self._start()

    def _start(self):
        self.reader = TrackDevicesReader()
        try:
            self.proc = Gio.Subprocess.new([self.adb, "track-devices"],
                                           Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as e:
            log.warning("Could not run adb: %s", e.message)
            return
        self._read()

    def _read(self):
        self.proc.get_stdout_pipe().read_bytes_async(4096, GLib.PRIORITY_DEFAULT, None, self._on_data)

    def _on_data(self, stream, result):
        try:
            data = stream.read_bytes_finish(result).get_data()
        except GLib.Error as e:
            log.warning("adb track-devices failed: %s", e.message)
            data = b""
        if not data:
            self._set_device(None)
            if not self.closing:
                log.info("adb track-devices exited; restarting in %d s", ADB_RESTART_S)
                GLib.timeout_add_seconds(ADB_RESTART_S, lambda: self._start() or GLib.SOURCE_REMOVE)
            return
        for devices in self.reader.feed(data):
            self._set_device(pick_adb_device(devices))
        self._read()

    def _set_device(self, serial):
        if serial == self.serial:
            return
        for source in self.timers:
            GLib.source_remove(source)
        self.timers = []
        self.tracker.reset()
        self.serial = serial
        self.service.set_adb_serial(serial)
        if serial:
            log.info("adb device attached: %s", serial)
            self._poll_battery()
            self._poll_notifications()
            self.timers = [GLib.timeout_add_seconds(ADB_BATTERY_S, self._poll_battery),
                           GLib.timeout_add_seconds(ADB_NOTIFICATIONS_S, self._poll_notifications)]
        else:
            log.info("No adb device")

    def _shell(self, command, callback):
        serial = self.serial

        def done(ok, out, err):
            if serial != self.serial:
                return
            if ok:
                callback(out)
            else:
                log.debug("adb shell %s failed: %s", " ".join(command), err.strip())

        run_async([self.adb, "-s", serial, "shell", *command], done)

    def _poll_battery(self):
        self._shell(["dumpsys", "battery"], lambda out: self.service.set_adb_battery(parse_battery(out)))
        return GLib.SOURCE_CONTINUE

    def _poll_notifications(self):
        def got(out):
            for record in self.tracker.new(parse_notifications(out)):
                # App and route only: the title and text stay out of the journal
                log.info("Relayed a notification over adb from %s", record["pkg"])
                self.service.notifier.show(record["title"] or app_label(record["pkg"]), record["text"],
                                           app_name=app_label(record["pkg"]))

        self._shell(["dumpsys", "notification", "--noredact"], got)
        return GLib.SOURCE_CONTINUE

    def close(self):
        self.closing = True
        if self.proc:
            self.proc.force_exit()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--debug", action="store_true", help="log adb commands that fail")
    opts = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if opts.debug else logging.INFO, format="%(levelname)s: %(message)s")

    loop = GLib.MainLoop()
    watchers = []

    def on_bus_acquired(bus, _name):
        adb_path = find_adb()
        scrcpy_path = find_tool("scrcpy", "SCRCPY")
        log.info("adb: %s; scrcpy: %s", adb_path or "not found", scrcpy_path or "not found")
        service = Service(bus, Notifier(bus), adb_path, scrcpy_path)
        try:
            watchers.append(BluezWatcher(Gio.bus_get_sync(Gio.BusType.SYSTEM, None), bus, service))
        except GLib.Error as e:
            log.warning("Bluetooth not available: %s", e.message)
        if adb_path:
            watchers.append(AdbWatcher(adb_path, service))

    def on_name_lost(_bus, _name):
        log.error("Could not own %s (already running?)", BUS_NAME)
        loop.quit()

    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus_acquired, None, on_name_lost)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
    loop.run()
    for watcher in watchers:
        watcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
