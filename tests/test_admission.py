"""Tracker allowlist at the gateway: refusing unknown IMEIs at login."""

import unittest

from gps_gateway.admission import Admission, PostgresAdmission
from gps_gateway.commands import SessionRegistry
from gps_gateway.config import Config
from gps_gateway.protocol import PROTO_LOCATION, PROTO_LOGIN, build_frame
from gps_gateway.server import DeviceSession, build_admission

from .test_codec import SAMPLE_LOCATION
from .test_session import IMEI, IMEI_BYTES, FakeWriter, MemorySink, acks_in


class Allow(Admission):
    def __init__(self, allowed):
        self.allowed = set(allowed)
        self.asked: list[tuple[str, str]] = []

    async def admit(self, device_id, peer):
        self.asked.append((device_id, peer))
        return device_id in self.allowed


class FakePool:
    """Stands in for asyncpg's pool: gateway_admit() answers from a set."""

    def __init__(self, allowed=(), fail=False):
        self.allowed = set(allowed)
        self.fail = fail
        self.calls: list[tuple] = []

    async def fetchval(self, query, *args):
        self.calls.append(args)
        if self.fail:
            raise OSError("database unreachable")
        return args[0] in self.allowed


class TestSessionAdmission(unittest.IsolatedAsyncioTestCase):
    def make(self, allowed):
        self.sink = MemorySink()
        self.registry = SessionRegistry()
        self.admission = Allow(allowed)
        self.session = DeviceSession(
            self.sink, Config(sink="log"), registry=self.registry, admission=self.admission
        )
        self.writer = FakeWriter()

    async def feed(self, data):
        for frame in self.session.decoder.feed(data):
            await self.session._process_frame(frame, self.writer)

    async def test_an_allowed_tracker_logs_in_as_before(self):
        self.make({IMEI})
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        self.assertEqual(self.session.device_id, IMEI)
        self.assertEqual([a.protocol for a in acks_in(self.writer)], [PROTO_LOGIN])
        self.assertFalse(self.session.refused)
        self.assertEqual(self.admission.asked, [(IMEI, "127.0.0.1:5023")])

    async def test_an_unknown_tracker_is_refused_without_an_ack(self):
        self.make(set())
        await self.feed(build_frame(PROTO_LOGIN, serial=1, content=IMEI_BYTES))
        await self.feed(build_frame(PROTO_LOCATION, serial=2, content=SAMPLE_LOCATION))
        self.assertTrue(self.session.refused)
        self.assertIsNone(self.session.device_id)
        self.assertEqual(self.writer.sent, b"")
        self.assertEqual(self.sink.events, [])
        self.assertIsNone(self.registry.get(IMEI))


class TestPostgresAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_enforce_refuses_unknown_and_admits_known(self):
        pool = FakePool({IMEI})
        admission = PostgresAdmission(lambda: pool, "enforce")
        self.assertTrue(await admission.admit(IMEI, "1.2.3.4:5"))
        self.assertFalse(await admission.admit("999999999999999", "1.2.3.4:5"))

    async def test_an_allowed_tracker_is_remembered(self):
        pool = FakePool({IMEI})
        admission = PostgresAdmission(lambda: pool, "enforce")
        for _ in range(5):
            await admission.admit(IMEI, "1.2.3.4:5")
        self.assertEqual(len(pool.calls), 1)

    async def test_an_unknown_tracker_is_asked_about_every_time(self):
        # So every attempt is counted, and an approval takes effect at once.
        pool = FakePool()
        admission = PostgresAdmission(lambda: pool, "enforce")
        await admission.admit("999999999999999", "1.2.3.4:5")
        pool.allowed.add("999999999999999")
        self.assertTrue(await admission.admit("999999999999999", "1.2.3.4:5"))

    async def test_log_mode_admits_unknown_but_still_records_it(self):
        pool = FakePool()
        admission = PostgresAdmission(lambda: pool, "log")
        self.assertTrue(await admission.admit("999999999999999", "1.2.3.4:5"))
        self.assertEqual(len(pool.calls), 1)

    async def test_an_unreachable_database_admits(self):
        admission = PostgresAdmission(lambda: FakePool(fail=True), "enforce")
        self.assertTrue(await admission.admit(IMEI, "1.2.3.4:5"))


class TestBuildAdmission(unittest.TestCase):
    class QueueWithPool:
        pool = FakePool()

    def test_modes(self):
        queue = self.QueueWithPool()
        self.assertIsInstance(build_admission(Config(allowlist="enforce"), queue), PostgresAdmission)
        self.assertIsInstance(build_admission(Config(allowlist="log"), queue), PostgresAdmission)
        self.assertIs(type(build_admission(Config(allowlist="off"), queue)), Admission)

    def test_without_a_database_it_is_off(self):
        self.assertIs(type(build_admission(Config(sink="log"), object())), Admission)

    def test_an_unknown_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            build_admission(Config(allowlist="strict"), self.QueueWithPool())

    def test_env(self):
        self.assertEqual(Config.from_env({"GATEWAY_ALLOWLIST": " LOG "}).allowlist, "log")
        self.assertEqual(Config.from_env({}).allowlist, "enforce")


if __name__ == "__main__":
    unittest.main()
