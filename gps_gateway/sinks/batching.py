"""
Batching wrapper: many packets, one database write.

Every location packet used to be its own INSERT round trip, so write
throughput was capped by round-trip latency and a slow database stalled
every connection at once. This sits in front of a real sink and turns
`publish()` calls into `publish_many()` batches.

A batch is written once it holds batch_size events, or flush_interval_s after
its first event, whichever comes first. Under load batches fill and go out
immediately; the interval only bounds how long a quiet gateway holds fixes.

`publish()` returns once the event is queued, not once it is written, so the
session ACKs the device straight away. That is what makes a long interval
workable: a GT06 resends any packet it is not ACKed for promptly, and holding
ACKs for the whole interval would turn into duplicate rows. The cost is
durability -- fixes already ACKed but still queued are lost if the process
dies before they are flushed, and a failed write is logged, not retried,
because the device has already moved on.

Exactly one flusher, deliberately. notify_vehicle_motion (sql/schema.sql)
compares each new row against the device's previous one, so rows must land
in the order they arrived; parallel writers could reorder two fixes from the
same device.
"""

import asyncio
import logging

from ..models import LocationEvent
from .base import Sink

log = logging.getLogger(__name__)

_STOP = object()


class BatchingSink(Sink):
    def __init__(
        self,
        inner: Sink,
        batch_size: int = 500,
        flush_interval_s: float = 10.0,
        queue_max: int = 10_000,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if flush_interval_s < 0:
            raise ValueError("flush_interval_s must not be negative")
        # asyncio.Queue treats 0 as unbounded, which would silently remove the
        # backpressure this class exists to provide.
        if queue_max < 1:
            raise ValueError("queue_max must be at least 1")
        self._inner = inner
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue_max = queue_max
        self._queue: asyncio.Queue | None = None
        self._flusher: asyncio.Task | None = None
        self._closing = False
        self.batches_flushed = 0
        self.events_failed = 0

    @property
    def inner(self) -> Sink:
        return self._inner

    async def start(self) -> None:
        await self._inner.start()
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        self._closing = False
        self._flusher = asyncio.create_task(self._run(), name="gateway-batch-flusher")

    async def publish(self, event: LocationEvent) -> None:
        if self._queue is None or self._closing:
            raise RuntimeError("BatchingSink is not running")
        # Blocks when the queue is full. The session awaiting this stops
        # reading its socket, so a stalled database pushes back on the
        # devices over TCP instead of growing memory without bound.
        await self._queue.put(event)

    async def stop(self) -> None:
        if self._flusher is not None:
            self._closing = True
            # Queued behind everything already waiting, so it all gets flushed.
            await self._queue.put(_STOP)
            await self._flusher
            self._flusher = None
            self._drop_leftovers()
        await self._inner.stop()

    def _drop_leftovers(self) -> None:
        """A publish that was blocked on a full queue can land behind the stop marker."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not _STOP:
                dropped += 1
        if dropped:
            self.events_failed += dropped
            log.warning("Dropped %d fixes queued after shutdown began", dropped)

    async def _run(self) -> None:
        queue = self._queue
        loop = asyncio.get_running_loop()
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            batch = [item]
            stopping = False
            deadline = loop.time() + self._flush_interval_s
            while len(batch) < self._batch_size:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), remaining)
                    except asyncio.TimeoutError:
                        break
                if item is _STOP:
                    stopping = True
                    break
                batch.append(item)
            await self._flush(batch)
            if stopping:
                return

    async def _flush(self, events: list[LocationEvent]) -> None:
        try:
            results = await self._inner.publish_many(events)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.events_failed += len(events)
            log.exception("Lost a batch of %d fixes: the write failed", len(events))
        else:
            failed = [(event, error) for event, error in zip(events, results) if error is not None]
            if failed:
                self.events_failed += len(failed)
                event, error = failed[0]
                log.error(
                    "Lost %d of %d fixes in a batch (first: device %s: %s)",
                    len(failed),
                    len(events),
                    event.device_id,
                    error,
                )
        self.batches_flushed += 1
