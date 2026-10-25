"""Parser tests for galaxyphoned. Run with: python3 -m unittest discover tests"""

import importlib.util
import os
import tempfile
import unittest

_path = os.path.join(os.path.dirname(__file__), "..", "daemon", "galaxyphoned.py")
_spec = importlib.util.spec_from_file_location("galaxyphoned", _path)
phoned = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phoned)

DUMPSYS_BATTERY = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  health: 2
  present: true
  level: 87
  scale: 100
  voltage: 4310
  temperature: 301
"""

# Shape of `dumpsys notification --noredact` on Android 14+, trimmed
DUMPSYS_NOTIFICATION = """Current Notification Manager state:
  Notification List:
    NotificationRecord(0x0a1b2c3d: pkg=com.whatsapp user=UserHandle{0} id=1 tag=null importance=4 key=0|com.whatsapp|1|null|10234: Notification(channel=individual_chat_defaults pri=1 contentView=null vibrate=null sound=null defaults=0x0 flags=0x10 color=0xff075e54 category=msg groupKey=group_key_messages vis=PRIVATE))
      uid=10234 userId=0
      opPkg=com.whatsapp
      icon=Icon(typ=RESOURCE pkg=com.whatsapp id=0x7f080a1c)
      flags=0x10
      pri=1
      key=0|com.whatsapp|1|null|10234
      extras={
        android.title=String (Maria)
        android.text=String (Chego em 10 min)
        android.subText=null
      }
    NotificationRecord(0x0b2c3d4e: pkg=com.whatsapp user=UserHandle{0} id=2 tag=null importance=4 key=0|com.whatsapp|2|null|10234: Notification(channel=individual_chat_defaults pri=1 flags=0x210))
      flags=0x210
      extras={
        android.title=String (WhatsApp)
        android.text=String (2 new messages)
      }
    NotificationRecord(0x0c3d4e5f: pkg=com.spotify.music user=UserHandle{0} id=3 tag=null importance=2 key=0|com.spotify.music|3|null|10301: Notification(channel=playback pri=0 flags=0x62))
      flags=0x62
      extras={
        android.title=String (Song)
        android.text=String (Artist)
      }
    NotificationRecord(0x0d4e5f60: pkg=com.google.android.gm user=UserHandle{0} id=4 tag=null importance=3 key=0|com.google.android.gm|4|null|10150: Notification(channel=mail pri=0 flags=0x18))
      flags=0x18
      extras={
        android.title=SpannableString (Search Tecnologia)
        android.text=SpannableString (Reunião amanhã)
        android.bigText=SpannableString (Reunião amanhã às 10h na sala 2)
      }
  mEnqueuedNotifications:
    NotificationRecord(0x0a1b2c3d: pkg=com.whatsapp user=UserHandle{0} id=1 tag=null importance=4 key=0|com.whatsapp|1|null|10234: Notification(channel=individual_chat_defaults))
      flags=0x0
"""


class TrackDevicesTests(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(phoned.TrackDevicesReader().feed(b"0000"), [[]])

    def test_split_blocks(self):
        reader = phoned.TrackDevicesReader()
        line = b"R5CX123ABC\tdevice\n"
        data = b"%04x" % len(line) + line + b"0000"
        self.assertEqual(reader.feed(data[:7]), [])
        self.assertEqual(reader.feed(data[7:]), [[("R5CX123ABC", "device")], []])

    def test_pick_device_skips_unauthorized_and_emulators(self):
        devices = [("emulator-5554", "device"), ("R5CX123ABC", "unauthorized"),
                   ("adb-R5CX123ABC-xyz._adb-tls-connect._tcp", "device")]
        self.assertEqual(phoned.pick_adb_device(devices), "adb-R5CX123ABC-xyz._adb-tls-connect._tcp")
        self.assertIsNone(phoned.pick_adb_device(devices[:2]))


class BatteryTests(unittest.TestCase):
    def test_charging(self):
        self.assertEqual(phoned.parse_battery(DUMPSYS_BATTERY), (87, True))

    def test_not_charging(self):
        self.assertEqual(phoned.parse_battery(DUMPSYS_BATTERY.replace("status: 2", "status: 3")), (87, False))

    def test_garbage(self):
        self.assertIsNone(phoned.parse_battery("error: device offline"))


class NotificationTests(unittest.TestCase):
    def test_parse(self):
        records = {r["key"]: r for r in phoned.parse_notifications(DUMPSYS_NOTIFICATION)}
        self.assertEqual(len(records), 4)
        chat = records["0|com.whatsapp|1|null|10234"]
        self.assertEqual((chat["pkg"], chat["title"], chat["text"], chat["flags"]),
                         ("com.whatsapp", "Maria", "Chego em 10 min", 0x10))
        self.assertEqual(records["0|com.google.android.gm|4|null|10150"]["text"], "Reunião amanhã às 10h na sala 2")

    def test_forwardable(self):
        keys = [r["key"] for r in phoned.parse_notifications(DUMPSYS_NOTIFICATION) if phoned.forwardable(r)]
        # Group summary (0x200) and the ongoing media notification (0x2 | 0x40) are dropped
        self.assertEqual(keys, ["0|com.whatsapp|1|null|10234", "0|com.google.android.gm|4|null|10150"])

    def test_tracker_primes_then_reports_new_and_changed(self):
        tracker = phoned.NotificationTracker()
        records = phoned.parse_notifications(DUMPSYS_NOTIFICATION)
        self.assertEqual(tracker.new(records), [])
        self.assertEqual(tracker.new(records), [])
        changed = phoned.parse_notifications(DUMPSYS_NOTIFICATION.replace("Chego em 10 min", "Cheguei"))
        self.assertEqual([r["text"] for r in tracker.new(changed)], ["Cheguei"])

    def test_app_label(self):
        self.assertEqual(phoned.app_label("com.whatsapp"), "WhatsApp")
        self.assertEqual(phoned.app_label("com.example.my_app"), "My app")


class PhoneDetectionTests(unittest.TestCase):
    def test_is_phone(self):
        self.assertTrue(phoned.is_phone({"Paired": True, "Icon": "phone"}))
        self.assertFalse(phoned.is_phone({"Paired": True, "Icon": "audio-headset"}))
        self.assertFalse(phoned.is_phone({"Paired": False, "Icon": "phone"}))


class ScrcpyPidsTests(unittest.TestCase):
    def _proc(self, root, pid, argv):
        os.makedirs(os.path.join(root, pid))
        with open(os.path.join(root, pid, "cmdline"), "wb") as f:
            f.write(b"\0".join(argv) + b"\0")

    def test_scrcpy_pids(self):
        with tempfile.TemporaryDirectory() as root:
            self._proc(root, "100", [b"/usr/bin/bash"])
            self._proc(root, "101", [b"/usr/bin/vim", b"scrcpy"])
            os.makedirs(os.path.join(root, "self"))
            self.assertEqual(phoned.scrcpy_pids(root), [])
            self._proc(root, "102", [b"/home/u/.local/bin/scrcpy", b"-s", b"serial"])
            self.assertEqual(phoned.scrcpy_pids(root), [102])



class MethodErrorTests(unittest.TestCase):
    class Invocation:
        def return_dbus_error(self, name, message):
            self.error = (name, message)

    def test_adb_pair_bad_input_is_logged_without_the_code(self):
        service = phoned.Service.__new__(phoned.Service)
        service.adb_path = "/usr/bin/adb"
        invocation = self.Invocation()
        params = phoned.GLib.Variant("(ss)", ("192.168.0.10", "123456"))
        with self.assertLogs("galaxyphoned", "WARNING") as logs:
            service._on_method_call(None, None, None, None, "AdbPair", params, invocation)
        self.assertEqual(invocation.error[0], phoned.ERROR_PREFIX + "InvalidArgs")
        self.assertIn("AdbPair failed: Enter the IP address:port", logs.output[0])
        self.assertNotIn("123456", logs.output[0])


if __name__ == "__main__":
    unittest.main()
