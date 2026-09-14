import asyncio
import unittest

from gps_gateway.models import LocationEvent
from gps_gateway.sinks.base import Sink
from gps_gateway.sinks.batching import BatchingSink
from gps_gateway.sinks.postgres import is_row_error


def event(n: int) -> LocationEvent:
    return LocationEvent(
        device_id=f"dev-{n}",
        latitude=12.97,
        longitude=77.59,
        speed_kmh=n,
        course_deg=0,
        gps_fixed=True,
    )


class RecordingSink(Sink):
    """Records every batch it is handed; can reject chosen rows or fail outright."""

    def __init__(self, reject_devices=(), fail_batches=False):
        self.batches: list[list[LocationEvent]] = []
        self.reject_devices = set(reject_devices)
        self.fail_batches = fail_batches
        self.gate: asyncio.Event | None = None
        self.stopped = False

    async def publish_many(self, events):
        if self.gate is not None:
            await self.gate.wait()
        self.batches.append(list(events))
        if self.fail_batches:
            raise RuntimeError("database is down")
        return [
            RuntimeError("row rejected") if e.device_id in self.reject_devices else None
            for e in events
        ]

    async def stop(self):
        self.stopped = True

    @property
    def sizes(self) -> list[int]:
        return [len(batch) for batch in self.batches]


class TestBatchingSink(unittest.IsolatedAsyncioTestCase):
    async def running(self, inner=None, **options) -> tuple[BatchingSink, RecordingSink]:
        inner = inner or RecordingSink()
        sink = BatchingSink(inner, **{"batch_size": 100, "flush_interval_s": 0.05, **options})
        await sink.start()
        self.addAsyncCleanup(sink.stop)
        return sink, inner

    async def eventually(self, condition, timeout: float = 1.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not condition():
            if loop.time() > deadline:
                self.fail("condition was not met in time")
            await asyncio.sleep(0.01)

    async def test_publish_returns_once_queued_not_once_written(self):
        # The session ACKs when publish returns. Holding that for a 10-second
        # interval would make a GT06 resend the packet and duplicate the row.
        sink, inner = await self.running(flush_interval_s=30)
        await asyncio.wait_for(sink.publish(event(1)), timeout=0.5)
        self.assertEqual(inner.sizes, [])

    async def test_the_interval_writes_a_partial_batch(self):
        sink, inner = await self.running()
        await asyncio.gather(*(sink.publish(event(n)) for n in range(3)))
        await self.eventually(lambda: inner.sizes == [3])

    async def test_a_full_batch_is_written_without_waiting_for_the_interval(self):
        sink, inner = await self.running(batch_size=3, flush_interval_s=30)
        await asyncio.gather(*(sink.publish(event(n)) for n in range(3)))
        await self.eventually(lambda: inner.sizes == [3])

    async def test_batch_size_caps_each_write(self):
        sink, inner = await self.running(batch_size=4)
        await asyncio.gather(*(sink.publish(event(n)) for n in range(10)))
        await self.eventually(lambda: sum(inner.sizes) == 10)
        self.assertEqual(inner.sizes, [4, 4, 2])

    async def test_rows_are_written_in_the_order_they_were_published(self):
        # The motion trigger compares each row with the device's previous one,
        # so a reordered batch could fire a bogus start/stop notification.
        sink, inner = await self.running()
        await asyncio.gather(*(sink.publish(event(n)) for n in range(10)))
        await self.eventually(lambda: inner.sizes == [10])
        self.assertEqual([e.speed_kmh for e in inner.batches[0]], list(range(10)))

    async def test_a_rejected_row_is_logged_and_the_rest_are_kept(self):
        sink, inner = await self.running(inner=RecordingSink(reject_devices={"dev-1"}))
        with self.assertLogs("gps_gateway.sinks.batching", level="ERROR") as logs:
            await asyncio.gather(*(sink.publish(event(n)) for n in range(3)))
            await self.eventually(lambda: sink.batches_flushed == 1)
        self.assertEqual(sink.events_failed, 1)
        self.assertIn("dev-1", logs.output[0])

    async def test_a_failed_batch_is_logged_and_the_writer_keeps_going(self):
        inner = RecordingSink(fail_batches=True)
        sink, _ = await self.running(inner=inner)
        with self.assertLogs("gps_gateway.sinks.batching", level="ERROR"):
            await asyncio.gather(*(sink.publish(event(n)) for n in range(3)))
            await self.eventually(lambda: sink.batches_flushed == 1)
        self.assertEqual(sink.events_failed, 3)

        # One outage must not kill the flusher for the rest of the process.
        inner.fail_batches = False
        await sink.publish(event(9))
        await self.eventually(lambda: sink.batches_flushed == 2)
        self.assertEqual(sink.events_failed, 3)

    async def test_a_full_queue_makes_publish_wait(self):
        # Backpressure: with the writer stuck, publish must block rather than
        # buffer without limit, so the session stops reading its socket.
        inner = RecordingSink()
        inner.gate = asyncio.Event()
        sink, _ = await self.running(inner=inner, batch_size=1, flush_interval_s=0, queue_max=2)

        await sink.publish(event(1))  # taken by the flusher, which then blocks
        await self.eventually(lambda: sink._queue.empty())
        await sink.publish(event(2))
        await sink.publish(event(3))  # queue now full
        blocked = asyncio.create_task(sink.publish(event(4)))
        await asyncio.sleep(0.05)
        self.assertFalse(blocked.done())

        inner.gate.set()
        await asyncio.wait_for(blocked, timeout=1)

    async def test_stop_writes_everything_still_queued(self):
        inner = RecordingSink()
        # An interval this long means only stop() can get the batch written.
        sink = BatchingSink(inner, batch_size=100, flush_interval_s=30)
        await sink.start()
        await asyncio.gather(*(sink.publish(event(n)) for n in range(5)))

        await asyncio.wait_for(sink.stop(), timeout=1)
        self.assertEqual(inner.sizes, [5])
        self.assertTrue(inner.stopped)

    async def test_publish_after_stop_is_refused(self):
        sink = BatchingSink(RecordingSink())
        await sink.start()
        await sink.stop()
        with self.assertRaises(RuntimeError):
            await sink.publish(event(1))

    def test_rejects_settings_that_would_disable_it_silently(self):
        with self.assertRaises(ValueError):
            BatchingSink(RecordingSink(), batch_size=0)
        with self.assertRaises(ValueError):
            BatchingSink(RecordingSink(), flush_interval_s=-1)
        # asyncio.Queue(maxsize=0) is unbounded -- no backpressure at all.
        with self.assertRaises(ValueError):
            BatchingSink(RecordingSink(), queue_max=0)


class FlakySink(Sink):
    async def publish(self, event):
        if event.device_id == "dev-1":
            raise RuntimeError("bad row")


class TestDefaultPublishMany(unittest.IsolatedAsyncioTestCase):
    async def test_reports_each_event_without_raising(self):
        results = await FlakySink().publish_many([event(0), event(1), event(2)])
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], RuntimeError)
        self.assertIsNone(results[2])


class FakePostgresError(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class TestRowErrorClassification(unittest.TestCase):
    """Which COPY failures are worth retrying row by row."""

    def test_rejected_data_is_a_row_error(self):
        self.assertTrue(is_row_error(FakePostgresError("22003")))  # numeric out of range
        self.assertTrue(is_row_error(FakePostgresError("23505")))  # unique violation

    def test_a_value_that_cannot_be_encoded_is_a_row_error(self):
        # What asyncpg really raises for a speed past INT's range -- found
        # against a live database, where it used to fail the whole batch.
        self.assertTrue(is_row_error(OverflowError("value out of int32 range")))

    def test_an_unreachable_database_is_not(self):
        self.assertFalse(is_row_error(FakePostgresError("08006")))  # connection failure
        self.assertFalse(is_row_error(FakePostgresError("57P01")))  # admin shutdown
        self.assertFalse(is_row_error(OSError("connection refused")))


if __name__ == "__main__":
    unittest.main()
