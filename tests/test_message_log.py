"""The human-readable per-packet log (gps_gateway/message_log.py) and log files."""

import logging
import os
import struct
import tempfile
import unittest

from gps_gateway.config import MAX_LOG_RETENTION_DAYS, Config
from gps_gateway.logging_setup import MESSAGE_LOG_FILE_NAME, MESSAGE_LOGGER, configure_logging
from gps_gateway.message_log import describe, log_event, log_inbound
from gps_gateway.protocol.framing import Frame


def frame(protocol: int, content: bytes, serial: int = 7, crc_ok: bool = True) -> Frame:
    return Frame(protocol=protocol, content=content, serial=serial, crc=0, crc_ok=crc_ok)


def location(lat: float, lon: float, speed: int, course: int, satellites: int = 9) -> bytes:
    # 2026-09-30 05:55:30 UTC = 11:25:30 IST; north/east hemisphere, GPS fixed.
    flags = 0x1000 | 0x0400 | course
    return (
        bytes([26, 9, 30, 5, 55, 30, 0xC0 | satellites])
        + struct.pack(">II", round(lat * 1_800_000), round(lon * 1_800_000))
        + bytes([speed])
        + struct.pack(">H", flags)
        # cell: MCC 404, MNC 45, LAC 1234, CellID 56789
        + struct.pack(">HBH", 404, 45, 1234)
        + (56789).to_bytes(3, "big")
    )


class TestDescribe(unittest.TestCase):
    def test_login_names_the_imei(self):
        self.assertEqual(
            describe(frame(0x01, bytes.fromhex("0868720065896205")), None), "IMEI 868720065896205"
        )

    def test_location_reads_like_a_sentence(self):
        text = describe(frame(0x12, location(12.971234, 77.594321, 44, 267)), "868720065896205")
        for part in [
            "fixed 2026-09-30 11:25:30 IST",
            "12.971234, 77.594321",
            "44 km/h",
            "heading 267° W",
            "9 satellites",
            "GPS lock",
            "cell 404/45/1234/56789",
            "https://maps.google.com/?q=12.971234,77.594321",
        ]:
            self.assertIn(part, text)

    def test_zero_satellites_is_called_out(self):
        text = describe(frame(0x12, location(12.97, 77.59, 0, 0, satellites=0)), "dev")
        self.assertIn("NO GPS LOCK", text)

    def test_heartbeat_spells_out_the_status_bytes(self):
        # terminal info: GPS tracking on (0x40) + charging (0x04) + ACC on (0x02)
        text = describe(frame(0x13, bytes([0x46, 4, 3, 0, 2])), "dev")
        self.assertEqual(
            text,
            "ignition ON · GPS tracking on · charging · battery medium (4/6) · GSM good (3/4)",
        )

    def test_heartbeat_alarm_is_named(self):
        self.assertIn("ALARM: SOS", describe(frame(0x13, bytes([0x40, 4, 3, 0x01, 2])), "dev"))

    def test_command_reply_shows_the_trackers_words(self):
        content = bytes([4 + 8]) + struct.pack(">I", 42) + b"Success!" + b"\x00\x02"
        self.assertEqual(describe(frame(0x15, content), "dev"), "to command 42: 'Success!'")

    def test_unknown_protocol_is_shown_as_hex(self):
        self.assertIn(
            "not handled by this gateway · 2 bytes · abcd",
            describe(frame(0x99, b"\xab\xcd"), "dev"),
        )


class TestLogLines(unittest.TestCase):
    def test_inbound_line_has_direction_device_type_and_serial(self):
        with self.assertLogs(MESSAGE_LOGGER, level="INFO") as captured:
            log_inbound(
                frame(0x13, bytes([0x40, 4, 3, 0, 2]), serial=42), "868720065896205", "1.2.3.4:5"
            )
        line = captured.records[0].getMessage()
        self.assertTrue(line.startswith("IN   868720065896205  HEARTBEAT  #0042  ignition OFF"))

    def test_before_login_the_peer_stands_in_for_the_device(self):
        with self.assertLogs(MESSAGE_LOGGER, level="INFO") as captured:
            log_inbound(frame(0x01, bytes.fromhex("0868720065896205")), None, "1.2.3.4:5")
        self.assertIn("1.2.3.4:5", captured.records[0].getMessage())

    def test_raw_hex_only_when_asked(self):
        with self.assertLogs(MESSAGE_LOGGER, level="INFO") as captured:
            log_inbound(frame(0x13, b"\x40"), "dev", "p")
            log_inbound(frame(0x13, b"\x40"), "dev", "p", raw=True)
        self.assertNotIn("raw", captured.records[0].getMessage())
        self.assertIn("raw 1340", captured.records[1].getMessage())


class TestConfig(unittest.TestCase):
    def test_retention_defaults_to_and_never_exceeds_30_days(self):
        self.assertEqual(Config.from_env({}).log_retention_days, 30)
        self.assertEqual(
            Config.from_env({"GATEWAY_LOG_RETENTION_DAYS": "365"}).log_retention_days,
            MAX_LOG_RETENTION_DAYS,
        )
        self.assertEqual(Config.from_env({"GATEWAY_LOG_RETENTION_DAYS": "7"}).log_retention_days, 7)

    def test_message_log_can_be_switched_off(self):
        self.assertTrue(Config.from_env({}).message_log)
        self.assertFalse(Config.from_env({"GATEWAY_MESSAGE_LOG": "off"}).message_log)


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.messages = logging.getLogger(MESSAGE_LOGGER)
        self.saved = (
            list(self.root.handlers),
            list(self.messages.handlers),
            self.messages.propagate,
        )

    def tearDown(self):
        for handler in self.messages.handlers + self.root.handlers:
            if handler not in self.saved[0] + self.saved[1]:
                handler.close()
        self.root.handlers, self.messages.handlers = self.saved[0], self.saved[1]
        self.messages.propagate = self.saved[2]
        self.messages.disabled = False

    def test_packets_go_to_their_own_file_with_an_ist_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            configure_logging(Config(log_dir=tmp, log_retention_days=30))
            log_event("OUT", "868720065896205", "LOGIN OK", "accepted")
            for handler in self.messages.handlers:
                handler.flush()
            with open(os.path.join(tmp, MESSAGE_LOG_FILE_NAME), encoding="utf-8") as f:
                line = f.read().strip()
            self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d IST OUT  868720065896205")
            self.assertTrue(os.path.exists(os.path.join(tmp, "gateway.log")))
            rotating = [
                h for h in self.messages.handlers if getattr(h, "baseFilename", "").startswith(tmp)
            ]
            self.assertEqual(rotating[0].backupCount, 30)


if __name__ == "__main__":
    unittest.main()
