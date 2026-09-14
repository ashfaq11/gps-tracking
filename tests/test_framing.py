import unittest

from gps_gateway.protocol import PROTO_HEARTBEAT, PROTO_LOCATION, PROTO_LOGIN, build_frame
from gps_gateway.protocol.framing import FrameDecoder

LOGIN = build_frame(PROTO_LOGIN, serial=1, content=bytes.fromhex("0868120303372449"))
HEARTBEAT = build_frame(PROTO_HEARTBEAT, serial=2, content=b"\x04\x00\x01\x00\x01")
LOCATION = build_frame(PROTO_LOCATION, serial=3, content=b"\xAA" * 18)


class TestFrameDecoder(unittest.TestCase):
    def setUp(self):
        self.decoder = FrameDecoder()

    def test_decodes_a_single_frame(self):
        frames = self.decoder.feed(LOGIN)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].protocol, PROTO_LOGIN)
        self.assertEqual(frames[0].serial, 1)
        self.assertEqual(frames[0].content, bytes.fromhex("0868120303372449"))
        self.assertEqual(self.decoder.buffer, b"")

    def test_serial_is_read_separately_from_the_crc(self):
        # The two are adjacent on the wire; confusing them breaks every ACK.
        frame = self.decoder.feed(LOCATION)[0]
        self.assertEqual(frame.serial, 3)
        self.assertNotEqual(frame.serial, frame.crc)
        self.assertEqual(len(frame.content), 18)

    def test_content_excludes_serial_crc_and_stop_bytes(self):
        frame = self.decoder.feed(HEARTBEAT)[0]
        self.assertEqual(frame.content, b"\x04\x00\x01\x00\x01")

    def test_decodes_several_frames_from_one_read(self):
        frames = self.decoder.feed(LOGIN + HEARTBEAT + LOCATION)
        self.assertEqual(
            [f.protocol for f in frames], [PROTO_LOGIN, PROTO_HEARTBEAT, PROTO_LOCATION]
        )

    def test_waits_for_the_rest_of_a_split_frame(self):
        self.assertEqual(self.decoder.feed(LOGIN[:6]), [])
        frames = self.decoder.feed(LOGIN[6:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].serial, 1)

    def test_survives_a_start_marker_split_across_reads(self):
        # The classic off-by-one: a trailing 0x78 must not be discarded.
        self.assertEqual(self.decoder.feed(LOGIN[:1]), [])
        frames = self.decoder.feed(LOGIN[1:])
        self.assertEqual(len(frames), 1)

    def test_reassembles_a_stream_delivered_one_byte_at_a_time(self):
        stream = LOGIN + HEARTBEAT + LOCATION
        collected = []
        for i in range(len(stream)):
            collected.extend(self.decoder.feed(stream[i : i + 1]))
        self.assertEqual(len(collected), 3)

    def test_skips_leading_garbage(self):
        frames = self.decoder.feed(b"\x00\xff\x01" + LOGIN)
        self.assertEqual(len(frames), 1)

    def test_drops_a_frame_with_a_bad_crc(self):
        corrupt = bytearray(LOCATION)
        corrupt[-3] ^= 0xFF  # flip a CRC byte
        self.assertEqual(self.decoder.feed(bytes(corrupt)), [])
        self.assertEqual(self.decoder.dropped_frames, 1)

    def test_recovers_the_next_frame_after_a_corrupt_one(self):
        corrupt = bytearray(LOCATION)
        corrupt[-3] ^= 0xFF
        frames = self.decoder.feed(bytes(corrupt) + LOGIN)
        self.assertEqual([f.protocol for f in frames], [PROTO_LOGIN])

    def test_resyncs_past_a_frame_with_missing_stop_bytes(self):
        broken = LOCATION[:-2] + b"\x00\x00"
        frames = self.decoder.feed(broken + LOGIN)
        self.assertEqual([f.protocol for f in frames], [PROTO_LOGIN])

    def test_rejects_an_implausibly_short_length_field(self):
        frames = self.decoder.feed(b"\x78\x78\x02\x01\x0d\x0a" + LOGIN)
        self.assertEqual([f.protocol for f in frames], [PROTO_LOGIN])

    def test_decodes_an_extended_79_79_frame(self):
        content = b"\xBB" * 300
        body = bytes([PROTO_LOCATION]) + content + (9).to_bytes(2, "big")
        length = len(body) + 2
        from gps_gateway.protocol.crc import crc16_itu

        head = length.to_bytes(2, "big")
        crc = crc16_itu(head + body).to_bytes(2, "big")
        frame = b"\x79\x79" + head + body + crc + b"\x0d\x0a"

        frames = self.decoder.feed(frame)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].protocol, PROTO_LOCATION)
        self.assertEqual(frames[0].serial, 9)
        self.assertEqual(frames[0].content, content)

    def test_discards_an_unbounded_stream_of_garbage(self):
        decoder = FrameDecoder(max_buffer_bytes=64)
        decoder.feed(b"\x11" * 500)
        self.assertLessEqual(len(decoder.buffer), 64)

    def test_a_full_frame_survives_the_buffer_guard(self):
        decoder = FrameDecoder(max_buffer_bytes=64)
        frames = decoder.feed(b"\x11" * 500 + LOGIN)
        self.assertEqual(len(frames), 1)


if __name__ == "__main__":
    unittest.main()
