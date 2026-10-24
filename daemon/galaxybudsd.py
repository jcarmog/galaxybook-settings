#!/usr/bin/env python3
"""galaxybudsd - per-user D-Bus service for Samsung Galaxy Buds Pro and Buds3 Pro.

Opens the Buds' Bluetooth serial (RFCOMM) control channel through BlueZ's
profile API whenever the earbuds connect, decodes their status messages
(battery, wear state, noise control, equalizer, touch lock) and exposes them
on the session bus so the GTK app and the GNOME Shell extension can show and
change them. No root access is needed: the Buds belong to the user's session.

Protocol notes (reverse-engineered by the GalaxyBudsClient project):
    frame   = SOM(0xFD) | header(u16 LE) | id(u8) | payload | crc16(LE) | EOM(0xDD)
    header  = size & 0x3FF, where size = 1 (id) + len(payload) + 2 (crc)
    crc16   = CRC-16/XMODEM over id + payload
"""

import argparse
import logging
import re
import signal
import socket
import sys

from gi.repository import Gio, GLib

BUS_NAME = "io.github.galaxybook.Buds1"
OBJECT_PATH = "/io/github/galaxybook/Buds1"
IFACE = "io.github.galaxybook.Buds1"
ERROR_PREFIX = "io.github.galaxybook.Error."

BLUEZ = "org.bluez"
PROFILE_PATH = "/io/github/galaxybook/BudsProfile"
CONNECT_DELAY_MS = 1500
CONNECT_RETRIES = 3

SOM = 0xFD
EOM = 0xDD

MSG_STATUS_UPDATED = 0x60
MSG_EXTENDED_STATUS_UPDATED = 0x61
MSG_NOISE_CONTROLS_UPDATE = 0x77
MSG_NOISE_CONTROLS = 0x78
MSG_EQUALIZER = 0x86
MSG_MANAGER_INFO = 0x88
MSG_LOCK_TOUCHPAD = 0x90

NOISE_CONTROL_MODES = ["off", "anc", "ambient", "adaptive"]
EQUALIZER_PRESETS = ["off", "bass-boost", "soft", "dynamic", "clear", "treble-boost"]
PLACEMENTS = ["disconnected", "wearing", "idle", "in-case", "case-closed"]


class Model:
    """What differs between supported Buds models."""

    def __init__(self, name, name_pattern, modalias, uuid, noise_modes, advanced_touch_lock):
        self.name = name
        self.name_pattern = re.compile(name_pattern)
        self.modalias = modalias
        self.uuid = uuid
        self.noise_modes = noise_modes
        # Buds2 and later lock individual gestures; byte 10 of the extended status is a bitmask
        self.advanced_touch_lock = advanced_touch_lock


SPP_STANDARD_UUID = "00001101-0000-1000-8000-00805f9b34fb"
SPP_SAMSUNG_UUID = "2e73a4ad-332d-41fc-90e2-16bef06523f2"

MODELS = [
    Model("Galaxy Buds3 Pro", r"\bBuds3 Pro\b", "bluetooth:v0075pA013", SPP_SAMSUNG_UUID,
          ["anc", "adaptive", "ambient", "off"], advanced_touch_lock=True),
    Model("Galaxy Buds Pro", r"\bBuds Pro\b", None, SPP_STANDARD_UUID,
          ["anc", "ambient", "off"], advanced_touch_lock=False),
]


def find_model(props):
    name = props.get("Name", "") or props.get("Alias", "")
    modalias = props.get("Modalias", "")
    for model in MODELS:
        if model.name_pattern.search(name) or (model.modalias and modalias.startswith(model.modalias)):
            return model
    return None

INTROSPECTION_XML = f"""
<node>
  <interface name="{IFACE}">
    <property name="Connected" type="b" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Address" type="s" access="read"/>
    <property name="Model" type="s" access="read"/>
    <property name="NoiseControlModes" type="as" access="read"/>
    <property name="BatteryLeft" type="i" access="read"/>
    <property name="BatteryRight" type="i" access="read"/>
    <property name="BatteryCase" type="i" access="read"/>
    <property name="PlacementLeft" type="s" access="read"/>
    <property name="PlacementRight" type="s" access="read"/>
    <property name="NoiseControl" type="s" access="read"/>
    <property name="Equalizer" type="s" access="read"/>
    <property name="TouchpadLocked" type="b" access="read"/>
    <method name="SetNoiseControl"><arg name="mode" type="s" direction="in"/></method>
    <method name="SetEqualizer"><arg name="preset" type="s" direction="in"/></method>
    <method name="SetTouchpadLocked"><arg name="locked" type="b" direction="in"/></method>
  </interface>
</node>
"""

PROFILE_XML = """
<node>
  <interface name="org.bluez.Profile1">
    <method name="Release"/>
    <method name="NewConnection">
      <arg name="device" type="o" direction="in"/>
      <arg name="fd" type="h" direction="in"/>
      <arg name="properties" type="a{sv}" direction="in"/>
    </method>
    <method name="RequestDisconnection"><arg name="device" type="o" direction="in"/></method>
    <method name="Cancel"/>
  </interface>
</node>
"""

PROPERTY_TYPES = {
    "Connected": "b",
    "Name": "s",
    "Address": "s",
    "Model": "s",
    "NoiseControlModes": "as",
    "BatteryLeft": "i",
    "BatteryRight": "i",
    "BatteryCase": "i",
    "PlacementLeft": "s",
    "PlacementRight": "s",
    "NoiseControl": "s",
    "Equalizer": "s",
    "TouchpadLocked": "b",
}

DISCONNECTED_STATE = {
    "Connected": False,
    "Name": "",
    "Address": "",
    "Model": "",
    "NoiseControlModes": [],
    "BatteryLeft": -1,
    "BatteryRight": -1,
    "BatteryCase": -1,
    "PlacementLeft": "disconnected",
    "PlacementRight": "disconnected",
    "NoiseControl": "",
    "Equalizer": "",
    "TouchpadLocked": False,
}

log = logging.getLogger("galaxybudsd")


class DBusError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = ERROR_PREFIX + name


# -- protocol ---------------------------------------------------------------


def crc16(data):
    """CRC-16/XMODEM (poly 0x1021, init 0), as used by the Buds firmware."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def encode_frame(msg_id, payload=b""):
    body = bytes([msg_id]) + bytes(payload)
    size = len(body) + 2
    return (bytes([SOM]) + size.to_bytes(2, "little") + body
            + crc16(body).to_bytes(2, "little") + bytes([EOM]))


class FrameReader:
    """Splits the RFCOMM byte stream into (msg_id, payload) messages."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        messages = []
        while True:
            start = self.buffer.find(SOM)
            if start < 0:
                self.buffer.clear()
                break
            del self.buffer[:start]
            if len(self.buffer) < 3:
                break
            size = int.from_bytes(self.buffer[1:3], "little") & 0x3FF
            total = 1 + 2 + size + 1
            if size < 3:
                del self.buffer[0]
                continue
            if len(self.buffer) < total:
                break
            frame = bytes(self.buffer[:total])
            body, crc = frame[3:total - 3], int.from_bytes(frame[total - 3:total - 1], "little")
            if frame[-1] != EOM or crc16(body) != crc:
                log.debug("Dropping corrupt frame: %s", frame.hex())
                del self.buffer[0]
                continue
            del self.buffer[:total]
            messages.append((body[0], body[1:]))
        return messages


def _placement(value):
    return PLACEMENTS[value] if value < len(PLACEMENTS) else "unknown"


def _battery(level, placement):
    return -1 if placement == "disconnected" else level


def _lookup(names, value):
    return names[value] if value < len(names) else "unknown"


def decode_message(msg_id, payload, model):
    """Return the properties a message updates, or {} if it is not one we track."""
    p = payload
    if msg_id == MSG_EXTENDED_STATUS_UPDATED and len(p) >= 13:
        left, right = _placement(p[6] >> 4), _placement(p[6] & 0x0F)
        state = {
            "BatteryLeft": _battery(p[2], left),
            "BatteryRight": _battery(p[3], right),
            "PlacementLeft": left,
            "PlacementRight": right,
            "Equalizer": _lookup(EQUALIZER_PRESETS, p[9]),
            # Advanced lock: bit 7 set means touch controls are enabled
            "TouchpadLocked": not p[10] & 0x80 if model.advanced_touch_lock else p[10] == 1,
            "NoiseControl": _lookup(NOISE_CONTROL_MODES, p[12]),
        }
        # The Buds3 Pro send 0 here while STATUS_UPDATED carries the real case level
        if p[7]:
            state["BatteryCase"] = p[7]
        return state
    if msg_id == MSG_STATUS_UPDATED and len(p) >= 7:
        left, right = _placement(p[5] >> 4), _placement(p[5] & 0x0F)
        return {
            "BatteryLeft": _battery(p[1], left),
            "BatteryRight": _battery(p[2], right),
            "PlacementLeft": left,
            "PlacementRight": right,
            "BatteryCase": p[6],
        }
    if msg_id == MSG_NOISE_CONTROLS_UPDATE and len(p) >= 1:
        return {"NoiseControl": _lookup(NOISE_CONTROL_MODES, p[0])}
    return {}


def encode_touch_lock(locked, model, gesture_bits):
    """Payload for LOCK_TOUCHPAD. Advanced models take the enable flag followed by one
    byte per gesture, so the gestures are copied from the last extended status."""
    if not model.advanced_touch_lock:
        return [1 if locked else 0]
    single, double, triple, hold = (gesture_bits >> 3) & 1, (gesture_bits >> 2) & 1, (gesture_bits >> 1) & 1, gesture_bits & 1
    double_call, hold_call = (gesture_bits >> 4) & 1, (gesture_bits >> 5) & 1
    return [0 if locked else 1, single, double, triple, hold, double_call, hold_call]


# -- D-Bus service ----------------------------------------------------------


class Service:
    """Session-bus object holding the state of the connected Buds."""

    def __init__(self, bus):
        self.bus = bus
        self.cache = dict(DISCONNECTED_STATE)
        self.sock = None
        self.model = None
        self.reader = None
        self.watch_id = 0
        self.gesture_bits = 0x3F  # all gestures on until the Buds report otherwise
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self.bus.register_object(OBJECT_PATH, node.interfaces[0], self._on_method_call, self._on_get_property, None)

    # -- connection ---------------------------------------------------------

    def attach(self, sock, model, name, address):
        self.detach()
        self.sock = sock
        self.model = model
        self.sock.settimeout(2)
        self.reader = FrameReader()
        self.watch_id = GLib.io_add_watch(
            sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_readable)
        self.update({"Connected": True, "Name": name, "Address": address, "Model": model.name,
                     "NoiseControlModes": model.noise_modes})
        log.info("Connected to %s (%s, %s)", name, model.name, address)

    def detach(self):
        if self.watch_id:
            GLib.source_remove(self.watch_id)
            self.watch_id = 0
        if self.sock:
            log.info("Disconnected from %s", self.cache["Name"])
            self.sock.close()
            self.sock = None
        self.model = None
        self.update(DISCONNECTED_STATE)

    def _on_readable(self, _fd, condition):
        data = b""
        if condition & GLib.IO_IN:
            try:
                data = self.sock.recv(4096)
            except OSError as e:
                log.warning("Read failed: %s", e)
        if not data:
            self.watch_id = 0
            self.detach()
            return GLib.SOURCE_REMOVE
        for msg_id, payload in self.reader.feed(data):
            log.debug("<< %02x %s", msg_id, payload.hex())
            if msg_id == MSG_EXTENDED_STATUS_UPDATED and len(payload) > 10:
                self.gesture_bits = payload[10]
            self.update(decode_message(msg_id, payload, self.model))
        return GLib.SOURCE_CONTINUE

    def send(self, msg_id, payload):
        if not self.sock:
            raise DBusError("NotConnected", "Galaxy Buds are not connected")
        frame = encode_frame(msg_id, payload)
        log.debug(">> %s", frame.hex())
        try:
            self.sock.sendall(frame)
        except OSError as e:
            raise DBusError("Failed", f"Could not send to the Buds: {e}") from e

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

    # -- methods ------------------------------------------------------------

    def _on_method_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        (arg,) = params.unpack()
        try:
            if method == "SetNoiseControl":
                if self.model and arg not in self.model.noise_modes:
                    raise DBusError("InvalidArgs", f"Noise control mode must be one of: {', '.join(self.model.noise_modes)}")
                self.send(MSG_NOISE_CONTROLS, [self._index(NOISE_CONTROL_MODES, arg, "Noise control mode")])
                self.update({"NoiseControl": arg})
            elif method == "SetEqualizer":
                self.send(MSG_EQUALIZER, [self._index(EQUALIZER_PRESETS, arg, "Equalizer preset")])
                self.update({"Equalizer": arg})
            elif method == "SetTouchpadLocked":
                if not self.model:
                    raise DBusError("NotConnected", "Galaxy Buds are not connected")
                self.send(MSG_LOCK_TOUCHPAD, encode_touch_lock(arg, self.model, self.gesture_bits))
                self.update({"TouchpadLocked": arg})
            else:
                raise DBusError("UnknownMethod", method)
        except DBusError as e:
            invocation.return_dbus_error(e.name, str(e))
            return
        log.info("%s(%r)", method, arg)
        invocation.return_value(None)

    @staticmethod
    def _index(names, value, what):
        if value not in names:
            raise DBusError("InvalidArgs", f"{what} must be one of: {', '.join(names)}")
        return names.index(value)


class BluezConnector:
    """Registers a serial-port client profile with BlueZ for each control-channel UUID
    and connects it to supported Buds as soon as their audio connection comes up."""

    def __init__(self, system_bus, service):
        self.bus = system_bus
        self.service = service
        self.device_path = None
        self.pending = {}  # device path -> GLib source id of a scheduled connect
        self.profile_paths = []
        node = Gio.DBusNodeInfo.new_for_xml(PROFILE_XML)
        for i, uuid in enumerate(sorted({m.uuid for m in MODELS})):
            path = f"{PROFILE_PATH}/{i}"
            self.bus.register_object(path, node.interfaces[0], self._on_profile_call, None, None)
            self.bus.call_sync(
                BLUEZ, "/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile",
                GLib.Variant("(osa{sv})", (path, uuid, {
                    "Name": GLib.Variant("s", "Galaxy Buds control"),
                    "Role": GLib.Variant("s", "client"),
                    "AutoConnect": GLib.Variant("b", False),
                })),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            self.profile_paths.append(path)
        self.bus.signal_subscribe(
            BLUEZ, "org.freedesktop.DBus.Properties", "PropertiesChanged", None, "org.bluez.Device1",
            Gio.DBusSignalFlags.NONE, self._on_device_changed)
        for path, props in self._devices():
            model = self._supported(props)
            if props.get("Connected") and model:
                self._schedule_connect(path, model)

    def _devices(self):
        reply = self.bus.call_sync(
            BLUEZ, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
            None, GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, -1, None)
        for path, ifaces in reply.unpack()[0].items():
            if "org.bluez.Device1" in ifaces:
                yield path, ifaces["org.bluez.Device1"]

    def _device_props(self, path):
        reply = self.bus.call_sync(
            BLUEZ, path, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", ("org.bluez.Device1",)),
            GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, -1, None)
        return reply.unpack()[0]

    @staticmethod
    def _supported(props):
        """The Model of a paired device whose control channel we can open, else None."""
        model = find_model(props)
        if model and props.get("Paired", False) and model.uuid in props.get("UUIDs", []):
            return model
        return None

    def _on_device_changed(self, _bus, _sender, path, _iface, _signal, params):
        _iface_name, changed, _invalidated = params.unpack()
        if "Connected" not in changed:
            return
        if changed["Connected"]:
            try:
                model = self._supported(self._device_props(path))
            except GLib.Error as e:
                log.warning("Could not read %s: %s", path, e.message)
                return
            if model:
                self._schedule_connect(path, model)
        else:
            if path in self.pending:
                GLib.source_remove(self.pending.pop(path))
            if path == self.device_path:
                self.device_path = None
                self.service.detach()

    def _schedule_connect(self, path, model, attempt=1):
        if path in self.pending or path == self.device_path:
            return

        def connect():
            del self.pending[path]
            log.info("Opening %s control channel to %s (attempt %d)", model.name, path, attempt)
            self.bus.call(
                BLUEZ, path, "org.bluez.Device1", "ConnectProfile", GLib.Variant("(s)", (model.uuid,)),
                None, Gio.DBusCallFlags.NONE, 30000, None, done)
            return GLib.SOURCE_REMOVE

        def done(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as e:
                log.warning("ConnectProfile failed: %s", e.message)
                if attempt < CONNECT_RETRIES and path != self.device_path:
                    self._schedule_connect(path, model, attempt + 1)

        self.pending[path] = GLib.timeout_add(CONNECT_DELAY_MS * attempt, connect)

    def _on_profile_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        if method == "NewConnection":
            path, fd_index, _props = params.unpack()
            sock = socket.socket(fileno=invocation.get_message().get_unix_fd_list().get(fd_index))
            try:
                props = self._device_props(path)
            except GLib.Error:
                props = {}
            model = find_model(props)
            if model:
                self.device_path = path
                self.service.attach(sock, model, props.get("Alias") or props.get("Name", ""), props.get("Address", ""))
            else:
                log.warning("Rejecting connection from unsupported device %s", path)
                sock.close()
        elif method == "RequestDisconnection":
            (path,) = params.unpack()
            if path == self.device_path:
                self.device_path = None
                self.service.detach()
        invocation.return_value(None)

    def close(self):
        for path in self.profile_paths:
            try:
                self.bus.call_sync(BLUEZ, "/org/bluez", "org.bluez.ProfileManager1", "UnregisterProfile",
                                   GLib.Variant("(o)", (path,)), None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--debug", action="store_true", help="log every message to and from the Buds")
    opts = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if opts.debug else logging.INFO, format="%(levelname)s: %(message)s")

    loop = GLib.MainLoop()
    connector = None

    def on_bus_acquired(bus, _name):
        nonlocal connector
        service = Service(bus)
        try:
            connector = BluezConnector(Gio.bus_get_sync(Gio.BusType.SYSTEM, None), service)
        except GLib.Error as e:
            log.error("Could not register with BlueZ: %s", e.message)
            loop.quit()

    def on_name_lost(_bus, _name):
        log.error("Could not own %s (already running?)", BUS_NAME)
        loop.quit()

    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus_acquired, None, on_name_lost)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
    loop.run()
    if connector:
        connector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
