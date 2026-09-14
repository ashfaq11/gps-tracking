import unittest
from datetime import datetime, timezone

from gps_gateway.protocol import build_ack, decode_location, decode_login

# A real GT06 location payload (Shenzhen, 2015-12-29 02:51:05 UTC):
#   0F 0C 1D 02 33 05  date/time
#   C9                 GPS info length 12, 9 satellites
#   02 7A C8 1F        latitude,  raw minutes * 30000
#   0C 46 58 60        longitude, raw minutes * 30000
#   00                 speed km/h
#   14 00              course + status flags
SAMPLE_LOCATION = bytes.fromhex("0F0C1D023305" "C9" "027AC81F" "0C465860" "00" "1400")


def location_payload(flags: int, lat_raw: int = 0x027AC81F, lon_raw: int = 0x0C465860) -> bytes:
    return (
        bytes.fromhex("0F0C1D023305")
        + bytes([0xC9])
        + lat_raw.to_bytes(4, "big")
        + lon_raw.to_bytes(4, "big")
        + bytes([0])
        + flags.to_bytes(2, "big")
    )


class TestDecodeLogin(unittest.TestCase):
    def test_strips_the_single_bcd_pad_nibble(self):
        # 0 + 15-digit IMEI packed into 8 BCD bytes.
        self.assertEqual(decode_login(bytes.fromhex("0868120303372449")), "868120303372449")

    def test_keeps_a_genuine_zero_after_the_pad(self):
        # Only one pad nibble may be dropped; the IMEI itself starts with 0.
        self.assertEqual(decode_login(bytes.fromhex("0086812030337244")), "086812030337244")

    def test_rejects_short_content(self):
        self.assertIsNone(decode_login(b"\x01\x02\x03"))

    def test_rejects_non_bcd_content(self):
        self.assertIsNone(decode_login(bytes.fromhex("AABBCCDDEEFF0011")))

    def test_ignores_trailing_extra_fields(self):
        content = bytes.fromhex("0868120303372449") + b"\x00\x36\x01\x02"
        self.assertEqual(decode_login(content), "868120303372449")


class TestDecodeLocation(unittest.TestCase):
    def test_decodes_the_documented_sample_packet(self):
        event = decode_location(SAMPLE_LOCATION, "dev1")
        self.assertIsNotNone(event)
        # Northern and eastern hemisphere -> both coordinates positive.
        self.assertAlmostEqual(event.latitude, 23.11, places=2)
        self.assertAlmostEqual(event.longitude, 114.4, places=1)
        self.assertEqual(event.speed_kmh, 0)
        self.assertEqual(event.course_deg, 0)
        self.assertTrue(event.gps_fixed)
        self.assertEqual(event.satellites, 9)
        self.assertEqual(event.device_id, "dev1")
        self.assertEqual(event.event_type, "location")

    def test_fixed_point_scale_is_minutes_times_30000(self):
        # 1 degree = 60 minutes = 1_800_000 raw units. Exact by construction,
        # so this pins the scale without leaning on a sample packet.
        event = decode_location(location_payload(flags=0x1400, lat_raw=1_800_000), "dev1")
        self.assertEqual(event.latitude, 1.0)

    def test_decodes_device_timestamp_as_utc(self):
        event = decode_location(SAMPLE_LOCATION, "dev1")
        self.assertEqual(event.fixed_at, datetime(2015, 12, 29, 2, 51, 5, tzinfo=timezone.utc))

    def test_tolerates_an_unset_device_clock(self):
        payload = b"\x00" * 6 + SAMPLE_LOCATION[6:]
        event = decode_location(payload, "dev1")
        self.assertIsNotNone(event)
        self.assertIsNone(event.fixed_at)

    def test_east_longitude_stays_positive(self):
        # Bit 11 clear = east. India, the target market, is east of Greenwich.
        event = decode_location(location_payload(flags=0x1400), "dev1")
        self.assertGreater(event.longitude, 0)

    def test_west_longitude_is_negative(self):
        event = decode_location(location_payload(flags=0x1C00), "dev1")
        self.assertLess(event.longitude, 0)

    def test_south_latitude_is_negative(self):
        event = decode_location(location_payload(flags=0x1000), "dev1")
        self.assertLess(event.latitude, 0)

    def test_course_uses_only_the_low_ten_bits(self):
        event = decode_location(location_payload(flags=0x1400 | 359), "dev1")
        self.assertEqual(event.course_deg, 359)

    def test_gps_fix_flag_is_independent_of_satellite_count(self):
        # Bit 12 clear means no fix even though 9 satellites are in view.
        event = decode_location(location_payload(flags=0x0400), "dev1")
        self.assertFalse(event.gps_fixed)
        self.assertEqual(event.satellites, 9)

    def test_rejects_truncated_payload(self):
        self.assertIsNone(decode_location(SAMPLE_LOCATION[:17], "dev1"))

    def test_rejects_out_of_range_coordinates(self):
        # A corrupt latitude that decodes past the pole must not be stored.
        self.assertIsNone(decode_location(location_payload(flags=0x1400, lat_raw=0xFFFFFFFF), "dev1"))


class TestBuildAck(unittest.TestCase):
    def test_matches_the_documented_login_ack(self):
        self.assertEqual(build_ack(0x01, 1), bytes.fromhex("7878050100 01D9DC0D0A"))

    def test_echoes_the_packet_serial(self):
        ack = build_ack(0x12, 0x1234)
        self.assertEqual(ack[4:6], b"\x12\x34")

    def test_length_byte_counts_protocol_serial_and_crc(self):
        ack = build_ack(0x13, 7)
        self.assertEqual(ack[2], 5)
        self.assertEqual(len(ack), 10)


if __name__ == "__main__":
    unittest.main()
