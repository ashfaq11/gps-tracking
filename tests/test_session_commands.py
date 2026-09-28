"""Gateway side of engine cut-off: delivering commands and matching replies."""

import asyncio
import unittest

from gps_gateway.commands import CommandQueue, PendingCommand, SessionRegistry, reply_is_failure
from gps_gateway.config import Config
from gps_gateway.protocol import (
    PROTO_COMMAND,
    PROTO_COMMAND_REPLY,
    PROTO_COMMAND_REPLY_NEW,
    PROTO_HEARTBEAT,
    PROTO_LOGIN,
    build_frame,
)
from gps_gateway.protocol.framing import FrameDecoder
from gps_gateway.server import DeviceSession

from .test_session import IMEI, IMEI_BYTES, FakeWriter, MemorySink

ACC_ON = b"\x02\x04\x04\x00\x02"
ACC_OFF = b"\x00\x04\x04\x00\x02"


class FakeQueue(CommandQueue):
    """Stands in for device_commands + claim_device_commands()."""

    def __init__(self, pending=None):
        self.pending: list[PendingCommand] = list(pending or [])
        self.claims: list[str] = []
        self.completed: list[tuple] = []

    async def claim(self, device_id):
        self.claims.append(device_id)
        mine = [c for c in self.pending if c.device_id == device_id]
        self.pending = [c for c in self.pending if c.device_id != device_id]
        return mine

    async def complete(self, command_id, device_id, *, ok, reply=None, error=None):
        self.completed.append((command_id, device_id, ok, reply, error))


def cut(command_id=41, device_id=IMEI):
    return PendingCommand(command_id, device_id, "cut", "RELAY,1#")


def restore(command_id=42, device_id=IMEI):
    return PendingCommand(command_id, device_id, "restore", "RELAY,0#")


def reply_frame(flag: int, text: str) -> bytes:
    raw = text.encode()
    return build_frame(
        PROTO_COMMAND_REPLY, serial=9, content=bytes([4 + len(raw)]) + flag.to_bytes(4, "big") + raw
    )


class TestCommandDelivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = FakeQueue()
        self.registry = SessionRegistry()
        self.config = Config(sink="log", command_reply_timeout_s=0.05)
        self.session = DeviceSession(MemorySink(), self.config, self.queue, self.registry)
        self.writer = FakeWriter()

    async def feed(self, data: bytes):
        for frame in self.session.decoder.feed(data):
            await self.session._process_frame(frame, self.writer)

    async def settle(self):
        # Let background delivery tasks run.
        for _ in range(5):
            await asyncio.sleep(0)

    def commands_sent(self):
        return [f for f in FrameDecoder().feed(self.writer.sent) if f.protocol == PROTO_COMMAND]

    async def login(self):
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        await self.settle()

    async def test_login_registers_the_session_and_checks_for_queued_commands(self):
        await self.login()
        self.assertIs(self.registry.get(IMEI), self.session)
        self.assertEqual(self.queue.claims, [IMEI])

    async def test_a_queued_cut_is_sent_with_its_id_as_the_server_flag(self):
        self.queue.pending = [cut(41)]
        await self.feed(build_frame(PROTO_HEARTBEAT, serial=1, content=ACC_OFF))  # before login: ignored
        await self.login()
        await self.feed(build_frame(PROTO_HEARTBEAT, serial=2, content=ACC_OFF))
        [frame] = self.commands_sent()
        self.assertEqual(frame.content[1:5], (41).to_bytes(4, "big"))
        self.assertEqual(frame.content[5:13], b"RELAY,1#")

    async def test_a_notification_delivers_to_the_session_holding_the_tracker(self):
        await self.login()
        self.queue.pending = [restore(42)]
        self.registry.notify(IMEI)
        self.registry.notify("some-other-imei")
        await self.settle()
        self.assertEqual(len(self.commands_sent()), 1)

    async def test_a_cut_is_refused_when_this_connection_says_the_ignition_is_on(self):
        await self.login()
        await self.feed(build_frame(PROTO_HEARTBEAT, serial=2, content=ACC_ON))
        self.queue.pending = [cut(41)]
        self.registry.notify(IMEI)
        await self.settle()
        self.assertEqual(self.commands_sent(), [])
        self.assertEqual(self.queue.completed, [(41, IMEI, False, None, "ignition_on")])

    async def test_restore_goes_out_even_with_the_ignition_on(self):
        await self.login()
        await self.feed(build_frame(PROTO_HEARTBEAT, serial=2, content=ACC_ON))
        self.queue.pending = [restore(42)]
        self.registry.notify(IMEI)
        await self.settle()
        self.assertEqual(len(self.commands_sent()), 1)

    async def test_the_trackers_reply_confirms_its_command(self):
        self.queue.pending = [cut(41)]
        await self.login()
        await self.feed(reply_frame(41, "Cut off the fuel supply: Success!"))
        self.assertEqual(
            self.queue.completed, [(41, IMEI, True, "Cut off the fuel supply: Success!", None)]
        )
        # Answered, so no 'no_reply' later.
        await asyncio.sleep(0.08)
        self.assertEqual(len(self.queue.completed), 1)

    async def test_a_reply_that_reports_failure_fails_the_command(self):
        self.queue.pending = [cut(41)]
        await self.login()
        await self.feed(reply_frame(41, "Cut off the fuel supply: Fail!"))
        self.assertEqual(self.queue.completed[0][2:], (False, "Cut off the fuel supply: Fail!", "tracker_reported_failure"))

    async def test_the_newer_reply_format_is_understood(self):
        self.queue.pending = [restore(42)]
        await self.login()
        content = (42).to_bytes(4, "big") + b"\x01" + b"RELAY=0 OK"
        await self.feed(build_frame(PROTO_COMMAND_REPLY_NEW, serial=3, content=content))
        self.assertEqual(self.queue.completed, [(42, IMEI, True, "RELAY=0 OK", None)])

    async def test_silence_is_recorded_as_no_reply(self):
        self.queue.pending = [cut(41)]
        await self.login()
        await asyncio.sleep(0.1)
        self.assertEqual(self.queue.completed, [(41, IMEI, False, None, "no_reply")])

    async def test_a_replaced_session_does_not_unregister_the_new_one(self):
        await self.login()
        newer = DeviceSession(MemorySink(), self.config, self.queue, self.registry)
        for frame in newer.decoder.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES)):
            await newer._process_frame(frame, FakeWriter())
        self.registry.remove(IMEI, self.session)
        self.assertIs(self.registry.get(IMEI), newer)


class TestReplyWording(unittest.TestCase):
    def test_failure_words(self):
        self.assertTrue(reply_is_failure("Cut off the fuel supply: Fail!"))
        self.assertTrue(reply_is_failure("Invalid command"))
        self.assertFalse(reply_is_failure("Cut off the fuel supply: Success!"))
        self.assertFalse(reply_is_failure("Speed Limit, cut off after speed less than 20km/h"))


if __name__ == "__main__":
    unittest.main()
