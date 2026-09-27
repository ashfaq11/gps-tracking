import unittest
from datetime import datetime, timezone

from gps_gateway.protocol import (
    build_ack,
    decode_heartbeat,
    decode_lbs,
    decode_location,
    decode_login,
)

# A real GT06 location payload (Shenzhen, 2015-12-29 02:51:05 UTC):
#   0F 0C 1D 02 33 05  date/time
#   C9                 GPS info length 12, 9 satellites
#   02 7A C8 1F        latitude,  raw minutes * 30000
#   0C 46 58 60        longitude, raw minutes * 30000
#   00                 speed km/h
#   14 00              course + status flags
SAMPLE_LOCATION = bytes.fromhex("0F0C1D023305" "C9" "027AC81F" "0C465860" "00" "1400")

# Serving cell that follows the position in most location packets:
#   01 CC   MCC 460 (China)
#   00      MNC 0
#   28 7D   LAC 10365
#   00 1F B8  CellID 8120
SAMPLE_CELL = bytes.fromhex("01CC" "00" "287D" "001FB8")


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


class TestDecodeCell(unittest.TestCase):
    def test_a_bare_location_has_no_cell(self):
        event = decode_location(SAMPLE_LOCATION, "dev1")
        self.assertIsNone(event.mcc)
        self.assertIsNone(event.cell_id)

    def test_decodes_the_cell_after_the_position(self):
        event = decode_location(SAMPLE_LOCATION + SAMPLE_CELL, "dev1")
        self.assertEqual((event.mcc, event.mnc, event.lac, event.cell_id), (460, 0, 10365, 8120))
        # The position itself is untouched by the extra bytes.
        self.assertAlmostEqual(event.latitude, 23.11, places=2)

    def test_ignores_fields_after_the_cell(self):
        # ACC, upload mode and mileage follow on some models.
        event = decode_location(SAMPLE_LOCATION + SAMPLE_CELL + b"\x01\x00\x01", "dev1")
        self.assertEqual(event.cell_id, 8120)

    def test_alarm_packets_skip_the_lbs_length_byte(self):
        content = SAMPLE_LOCATION + b"\x09" + SAMPLE_CELL + b"\x44\x06\x04\x00\x01"
        event = decode_location(content, "dev1", lbs_length_prefix=True)
        self.assertEqual((event.mcc, event.mnc, event.lac, event.cell_id), (460, 0, 10365, 8120))

    def test_an_all_zero_cell_is_no_cell(self):
        # What a tracker with no GSM registration sends.
        event = decode_location(SAMPLE_LOCATION + b"\x00" * 8, "dev1")
        self.assertIsNone(event.mcc)
        self.assertIsNone(event.lac)

    def test_a_truncated_cell_is_dropped_but_the_fix_kept(self):
        event = decode_location(SAMPLE_LOCATION + SAMPLE_CELL[:5], "dev1")
        self.assertIsNotNone(event)
        self.assertIsNone(event.cell_id)

    def test_mcc_high_bit_announces_a_two_byte_mnc(self):
        # MCC 404 | 0x8000, MNC 0x0356 = 854, LAC 0x1005, CellID 0x005483.
        cell = bytes.fromhex("8194" "0356" "1005" "005483")
        event = decode_location(SAMPLE_LOCATION + cell, "dev1")
        self.assertEqual((event.mcc, event.mnc, event.lac, event.cell_id), (404, 854, 4101, 21635))


class TestIgnition(unittest.TestCase):
    def test_heartbeat_acc_bit_means_ignition_on(self):
        status = decode_heartbeat(bytes.fromhex("4604040001"), "dev1")
        self.assertEqual(status.device_id, "dev1")
        self.assertTrue(status.ignition)

    def test_heartbeat_without_the_acc_bit_is_off(self):
        # 0x44: GPS tracking + charging, but bit 1 (ACC) clear.
        self.assertFalse(decode_heartbeat(bytes.fromhex("4404040001"), "dev1").ignition)

    def test_an_empty_heartbeat_says_nothing(self):
        self.assertIsNone(decode_heartbeat(b"", "dev1"))

    def test_alarm_reads_ignition_after_the_cell(self):
        content = SAMPLE_LOCATION + b"\x09" + SAMPLE_CELL + b"\x46\x06\x04\x01\x01"
        event = decode_location(content, "dev1", lbs_length_prefix=True)
        self.assertTrue(event.ignition)
        self.assertEqual(event.cell_id, 8120)

    def test_alarm_without_a_cell_reads_ignition_right_after_the_length(self):
        # LBS length 0: no cell, and the status byte must not be misread as one.
        content = SAMPLE_LOCATION + b"\x00" + b"\x44\x06\x04\x01\x01"
        event = decode_location(content, "dev1", lbs_length_prefix=True)
        self.assertFalse(event.ignition)
        self.assertIsNone(event.mcc)

    def test_a_plain_location_does_not_report_ignition(self):
        self.assertIsNone(decode_location(SAMPLE_LOCATION + SAMPLE_CELL, "dev1").ignition)

    def test_an_alarm_cut_short_before_the_status_reports_none(self):
        content = SAMPLE_LOCATION + b"\x09" + SAMPLE_CELL
        self.assertIsNone(decode_location(content, "dev1", lbs_length_prefix=True).ignition)


class TestDecodeLbs(unittest.TestCase):
    STAMP = bytes.fromhex("1A091B0A0F1E")  # 2026-09-27 10:15:30 UTC

    def test_decodes_serving_cell_signal_and_time(self):
        neighbours = bytes.fromhex("287D001FB9" "2B") * 6
        content = self.STAMP + SAMPLE_CELL + b"\x3C" + neighbours + b"\xFF" + b"\x00\x02"
        report = decode_lbs(content, "dev1")
        self.assertEqual(report.device_id, "dev1")
        self.assertEqual((report.mcc, report.mnc, report.lac, report.cell_id), (460, 0, 10365, 8120))
        self.assertEqual(report.signal, 0x3C)
        self.assertEqual(report.reported_at, datetime(2026, 9, 27, 10, 15, 30, tzinfo=timezone.utc))

    def test_signal_is_optional(self):
        report = decode_lbs(self.STAMP + SAMPLE_CELL, "dev1")
        self.assertEqual(report.cell_id, 8120)
        self.assertIsNone(report.signal)

    def test_rejects_truncated_content(self):
        self.assertIsNone(decode_lbs(self.STAMP + SAMPLE_CELL[:7], "dev1"))

    def test_rejects_an_all_zero_cell(self):
        self.assertIsNone(decode_lbs(self.STAMP + b"\x00" * 9, "dev1"))


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
