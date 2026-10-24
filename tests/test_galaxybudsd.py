"""Protocol tests for galaxybudsd. Run with: python3 -m unittest discover tests"""

import importlib.util
import os
import unittest

_path = os.path.join(os.path.dirname(__file__), "..", "daemon", "galaxybudsd.py")
_spec = importlib.util.spec_from_file_location("galaxybudsd", _path)
budsd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budsd)

BUDS_PRO = budsd.MODELS[1]
BUDS3_PRO = budsd.MODELS[0]

# Captured from a Galaxy Buds3 Pro (firmware R630XXU0AZG2): left in ear, right out, ambient sound on
BUDS3_PRO_EXTENDED_STATUS = bytes.fromhex(
    "04086162010012000000ff2202005401540108000499000404100001000011020000"
    "000000000000000001000000000000020000000001010100000100")
BUDS3_PRO_STATUS = bytes.fromhex("02606101001158000000")


def extended_status(battery_l=80, battery_r=75, placement=0x13, case=60, eq=0, lock=0, noise=1):
    """Synthetic Buds Pro EXTENDED_STATUS_UPDATED payload (revision 10 layout)."""
    payload = bytearray(38)
    payload[0:8] = bytes([10, 2, battery_l, battery_r, 1, 1, placement, case])
    payload[9], payload[10], payload[11], payload[12] = eq, lock, 0x22, noise
    return bytes(payload)


class Crc16Tests(unittest.TestCase):
    def test_xmodem_check_value(self):
        self.assertEqual(budsd.crc16(b"123456789"), 0x31C3)


class FrameTests(unittest.TestCase):
    def test_encode_layout(self):
        frame = budsd.encode_frame(budsd.MSG_NOISE_CONTROLS, [1])
        self.assertEqual(frame[0], 0xFD)
        self.assertEqual(frame[-1], 0xDD)
        self.assertEqual(int.from_bytes(frame[1:3], "little"), 4)  # id + 1 payload byte + crc
        self.assertEqual(frame[3:5], bytes([0x78, 1]))
        self.assertEqual(int.from_bytes(frame[5:7], "little"), budsd.crc16(bytes([0x78, 1])))

    def test_round_trip_split_and_concatenated(self):
        stream = budsd.encode_frame(0x61, extended_status()) + budsd.encode_frame(0x77, [2])
        reader = budsd.FrameReader()
        messages = []
        for i in range(0, len(stream), 5):
            messages += reader.feed(stream[i:i + 5])
        self.assertEqual([m[0] for m in messages], [0x61, 0x77])
        self.assertEqual(messages[1][1], bytes([2]))

    def test_skips_garbage_and_corrupt_frames(self):
        good = budsd.encode_frame(0x77, [0])
        corrupt = bytearray(budsd.encode_frame(0x77, [1]))
        corrupt[4] ^= 0xFF
        messages = budsd.FrameReader().feed(b"\x00\x13" + bytes(corrupt) + good)
        self.assertEqual(messages, [(0x77, bytes([0]))])

    def test_ignores_flag_bits_in_header(self):
        frame = bytearray(budsd.encode_frame(0x77, [2]))
        frame[2] |= 0x0C
        self.assertEqual(budsd.FrameReader().feed(bytes(frame)), [(0x77, bytes([2]))])


class DecodeTests(unittest.TestCase):
    def test_extended_status(self):
        state = budsd.decode_message(0x61, extended_status(eq=3, lock=1, noise=2), BUDS_PRO)
        self.assertEqual(state, {
            "BatteryLeft": 80,
            "BatteryRight": 75,
            "PlacementLeft": "wearing",
            "PlacementRight": "in-case",
            "BatteryCase": 60,
            "Equalizer": "dynamic",
            "TouchpadLocked": True,
            "NoiseControl": "ambient",
        })

    def test_disconnected_bud_has_no_battery(self):
        state = budsd.decode_message(0x61, extended_status(placement=0x10), BUDS_PRO)
        self.assertEqual(state["BatteryRight"], -1)
        self.assertEqual(state["PlacementRight"], "disconnected")

    def test_status_update(self):
        payload = bytes([10, 55, 66, 1, 1, 0x22, 90])
        self.assertEqual(budsd.decode_message(0x60, payload, BUDS_PRO), {
            "BatteryLeft": 55,
            "BatteryRight": 66,
            "PlacementLeft": "idle",
            "PlacementRight": "idle",
            "BatteryCase": 90,
        })

    def test_noise_control_update(self):
        self.assertEqual(budsd.decode_message(0x77, bytes([1]), BUDS_PRO), {"NoiseControl": "anc"})

    def test_short_and_unknown_messages(self):
        self.assertEqual(budsd.decode_message(0x61, bytes(5), BUDS_PRO), {})
        self.assertEqual(budsd.decode_message(0x42, bytes(10), BUDS_PRO), {})


class Buds3ProTests(unittest.TestCase):
    def test_extended_status_capture(self):
        self.assertEqual(budsd.decode_message(0x61, BUDS3_PRO_EXTENDED_STATUS, BUDS3_PRO), {
            "BatteryLeft": 97,
            "BatteryRight": 98,
            "PlacementLeft": "wearing",
            "PlacementRight": "idle",
            "Equalizer": "off",
            "TouchpadLocked": False,
            "NoiseControl": "ambient",
        })

    def test_status_capture_has_case_battery(self):
        state = budsd.decode_message(0x60, BUDS3_PRO_STATUS, BUDS3_PRO)
        self.assertEqual((state["BatteryLeft"], state["BatteryRight"], state["BatteryCase"]), (96, 97, 88))

    def test_touch_lock_bit(self):
        payload = bytearray(BUDS3_PRO_EXTENDED_STATUS)
        payload[10] = 0x7F
        self.assertTrue(budsd.decode_message(0x61, bytes(payload), BUDS3_PRO)["TouchpadLocked"])

    def test_touch_lock_keeps_gestures(self):
        # Acknowledged by real Buds3 Pro: lock -> 00 01 01 01 01 01 01, unlock -> 01 01 01 01 01 01 01
        self.assertEqual(budsd.encode_touch_lock(True, BUDS3_PRO, 0xFF), [0, 1, 1, 1, 1, 1, 1])
        self.assertEqual(budsd.encode_touch_lock(False, BUDS3_PRO, 0xFF), [1, 1, 1, 1, 1, 1, 1])
        # single tap off (bit 3), hold on (bit 0)
        self.assertEqual(budsd.encode_touch_lock(False, BUDS3_PRO, 0x81), [1, 0, 0, 0, 1, 0, 0])
        self.assertEqual(budsd.encode_touch_lock(True, BUDS_PRO, 0), [1])


class ModelTests(unittest.TestCase):
    def test_find_model(self):
        self.assertIs(budsd.find_model({"Name": "José do Carmo's Buds3 Pro"}), BUDS3_PRO)
        self.assertIs(budsd.find_model({"Name": "Galaxy Buds Pro (AAAD)"}), BUDS_PRO)
        self.assertIs(budsd.find_model({"Name": "Earbuds", "Modalias": "bluetooth:v0075pA013d0001"}), BUDS3_PRO)
        self.assertIsNone(budsd.find_model({"Name": "Galaxy Buds2 Pro"}))
        self.assertIsNone(budsd.find_model({"Name": "JBL Flip 6"}))


if __name__ == "__main__":
    unittest.main()
