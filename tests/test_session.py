import asyncio
import unittest

from gps_gateway.config import Config
from gps_gateway.models import LocationEvent
from gps_gateway.protocol import (
    PROTO_ALARM,
    PROTO_HEARTBEAT,
    PROTO_LOCATION,
    PROTO_LOGIN,
    build_frame,
)
from gps_gateway.protocol.framing import FrameDecoder
from gps_gateway.server import DeviceSession
from gps_gateway.sinks.base import Sink
from gps_gateway.sinks.batching import BatchingSink

from .test_codec import SAMPLE_LOCATION

IMEI_BYTES = bytes.fromhex("0868120303372449")
IMEI = "868120303372449"


class MemorySink(Sink):
    def __init__(self, fail=False):
        self.events: list[LocationEvent] = []
        self.fail = fail

    async def publish(self, event):
        if self.fail:
            raise RuntimeError("sink is down")
        self.events.append(event)


class CountingSink(MemorySink):
    """A MemorySink that also records how many events each write carried."""

    def __init__(self):
        super().__init__()
        self.batch_sizes: list[int] = []

    async def publish_many(self, events):
        self.batch_sizes.append(len(events))
        return await super().publish_many(events)


class FakeWriter:
    """Stands in for asyncio.StreamWriter, recording what the server sends."""

    def __init__(self):
        self.sent = b""
        self.closed = False

    def write(self, data):
        self.sent += data

    async def drain(self):
        pass

    def get_extra_info(self, _name):
        return ("127.0.0.1", 5023)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def acks_in(writer: FakeWriter):
    return FrameDecoder().feed(writer.sent)


def run(coro):
    return asyncio.run(coro)


class TestDeviceSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = MemorySink()
        self.session = DeviceSession(self.sink, Config(sink="log"))
        self.writer = FakeWriter()

    async def feed(self, data: bytes):
        for frame in self.session.decoder.feed(data):
            await self.session._process_frame(frame, self.writer)

    async def test_login_is_acknowledged_with_the_matching_serial(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=0x0042, content=IMEI_BYTES))
        self.assertEqual(self.session.device_id, IMEI)
        acks = acks_in(self.writer)
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0].protocol, PROTO_LOGIN)
        self.assertEqual(acks[0].serial, 0x0042)

    async def test_every_ack_carries_a_valid_crc(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        await self.feed(build_frame(PROTO_HEARTBEAT, serial=2, content=b"\x04\x00\x01"))
        acks = acks_in(self.writer)
        self.assertEqual(len(acks), 2)
        self.assertTrue(all(a.crc_ok for a in acks))

    async def test_location_after_login_is_published(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        await self.feed(build_frame(PROTO_LOCATION, serial=2, content=SAMPLE_LOCATION))
        self.assertEqual(len(self.sink.events), 1)
        event = self.sink.events[0]
        self.assertEqual(event.device_id, IMEI)
        self.assertAlmostEqual(event.latitude, 23.11, places=2)
        self.assertGreater(event.longitude, 0)

    async def test_location_before_login_is_dropped_and_not_acked(self):
        await self.feed(build_frame(PROTO_LOCATION, serial=1, content=SAMPLE_LOCATION))
        self.assertEqual(self.sink.events, [])
        self.assertEqual(self.writer.sent, b"")

    async def test_alarm_packets_are_published_as_alarms(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        await self.feed(build_frame(PROTO_ALARM, serial=2, content=SAMPLE_LOCATION))
        self.assertEqual(len(self.sink.events), 1)
        self.assertEqual(self.sink.events[0].event_type, "alarm")

    async def test_heartbeat_is_acked_without_publishing(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        await self.feed(build_frame(PROTO_HEARTBEAT, serial=9, content=b"\x04\x00\x01"))
        self.assertEqual(self.sink.events, [])
        self.assertEqual(acks_in(self.writer)[-1].serial, 9)

    async def test_unknown_protocol_is_ignored(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        before = self.writer.sent
        await self.feed(build_frame(0x99, serial=2, content=b"\x01"))
        self.assertEqual(self.writer.sent, before)

    async def test_a_failing_sink_does_not_break_the_connection(self):
        session = DeviceSession(MemorySink(fail=True), Config(sink="log"))
        writer = FakeWriter()
        for frame in session.decoder.feed(
            build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES)
            + build_frame(PROTO_LOCATION, serial=2, content=SAMPLE_LOCATION)
        ):
            await session._process_frame(frame, writer)
        # The device is still ACKed, so it does not retry into a stuck loop.
        self.assertEqual(len(acks_in(writer)), 2)

    async def test_a_burst_is_acked_at_once_and_written_as_one_batch(self):
        # A tracker back in coverage uploads its buffered fixes in one burst.
        # Every packet must be ACKed without waiting out the flush interval --
        # a GT06 resends anything left unACKed -- and the fixes should still
        # reach the database as one write, not five.
        inner = CountingSink()
        sink = BatchingSink(inner, batch_size=100, flush_interval_s=30)
        await sink.start()
        self.addAsyncCleanup(sink.stop)
        session = DeviceSession(sink, Config(sink="log"))
        writer = FakeWriter()

        burst = build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES) + b"".join(
            build_frame(PROTO_LOCATION, serial=n, content=SAMPLE_LOCATION) for n in range(2, 7)
        )
        await asyncio.wait_for(
            session._process_frames(session.decoder.feed(burst), writer), timeout=1
        )

        self.assertEqual([ack.serial for ack in acks_in(writer)], [1, 2, 3, 4, 5, 6])
        self.assertEqual(inner.batch_sizes, [])  # ACKed while still queued

        await sink.stop()
        self.assertEqual(inner.batch_sizes, [5])

    async def test_unreadable_login_is_rejected(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=b"\xAA" * 8))
        self.assertIsNone(self.session.device_id)
        self.assertEqual(self.writer.sent, b"")


class TestServerEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_full_device_conversation_over_a_real_socket(self):
        sink = MemorySink()
        config = Config(host="127.0.0.1", port=0, sink="log", idle_timeout_s=5.0)

        async def client_connected(reader, writer):
            await DeviceSession(sink, config).handle(reader, writer)

        server = await asyncio.start_server(client_connected, config.host, config.port)
        port = server.sockets[0].getsockname()[1]

        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
                await writer.drain()
                login_ack = await asyncio.wait_for(reader.read(64), timeout=2)

                # Two location frames arriving inside a single TCP segment.
                writer.write(
                    build_frame(PROTO_LOCATION, serial=2, content=SAMPLE_LOCATION)
                    + build_frame(PROTO_LOCATION, serial=3, content=SAMPLE_LOCATION)
                )
                await writer.drain()
                loc_acks = await asyncio.wait_for(reader.read(64), timeout=2)
            finally:
                writer.close()
                await writer.wait_closed()

        self.assertEqual(acks_in(FakeWriterFrom(login_ack))[0].serial, 1)
        serials = [f.serial for f in FrameDecoder().feed(loc_acks)]
        self.assertEqual(serials, [2, 3])
        self.assertEqual(len(sink.events), 2)
        self.assertTrue(all(e.device_id == IMEI for e in sink.events))


class FakeWriterFrom(FakeWriter):
    def __init__(self, data):
        super().__init__()
        self.sent = data


if __name__ == "__main__":
    unittest.main()
