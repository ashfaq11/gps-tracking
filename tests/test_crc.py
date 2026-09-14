import unittest

from gps_gateway.protocol import crc16_itu


class TestCrc16Itu(unittest.TestCase):
    def test_standard_check_value(self):
        # The published CRC-16/X-25 check value for the ASCII digits 1..9.
        self.assertEqual(crc16_itu(b"123456789"), 0x906E)

    def test_matches_documented_gt06_ack_frame(self):
        # From the GT06 spec: 78 78 05 01 00 01 D9 DC 0D 0A
        # CRC covers the length byte through the serial number.
        self.assertEqual(crc16_itu(bytes.fromhex("05010001")), 0xD9DC)

    def test_empty_input(self):
        self.assertEqual(crc16_itu(b""), 0x0000)

    def test_detects_single_bit_flip(self):
        data = bytes.fromhex("0D0103510752534189330001")
        flipped = bytes([data[0] ^ 0x01]) + data[1:]
        self.assertNotEqual(crc16_itu(data), crc16_itu(flipped))


if __name__ == "__main__":
    unittest.main()
