"""Asyncio TCP server: one persistent connection per device."""

import asyncio
import logging
from typing import Optional

from .config import Config
from .models import LocationEvent
from .protocol import (
    PROTO_ALARM,
    PROTO_HEARTBEAT,
    PROTO_LOCATION,
    PROTO_LOGIN,
    build_ack,
    decode_location,
    decode_login,
)
from .protocol.framing import Frame, FrameDecoder
from .sinks import Sink, build_sink

log = logging.getLogger(__name__)

_READ_SIZE = 4096


class DeviceSession:
    """
    Per-connection state. A tracker logs in once, then streams location and
    heartbeat frames over the same socket until it loses signal.
    """

    def __init__(self, sink: Sink, config: Config):
        self.sink = sink
        self.config = config
        self.device_id: Optional[str] = None
        self.decoder = FrameDecoder(max_buffer_bytes=config.max_buffer_bytes)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Connection opened from %s", peer)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(_READ_SIZE), timeout=self.config.idle_timeout_s
                    )
                except asyncio.TimeoutError:
                    log.info("Idle timeout for device=%s peer=%s", self.device_id, peer)
                    break
                if not chunk:
                    break
                frames = self.decoder.feed(chunk)
                if frames:
                    await self._process_frames(frames, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError) as exc:
            log.info("Connection dropped (%s) for device=%s", exc, self.device_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One malformed device must not take down the listener.
            log.exception("Unhandled error for device=%s peer=%s", self.device_id, peer)
        finally:
            await self._close(writer)
            log.info(
                "Connection closed for device=%s peer=%s (dropped_frames=%d)",
                self.device_id,
                peer,
                self.decoder.dropped_frames,
            )

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    async def _process_frame(self, frame: Frame, writer: asyncio.StreamWriter) -> None:
        await self._process_frames([frame], writer)

    async def _process_frames(self, frames: list[Frame], writer: asyncio.StreamWriter) -> None:
        """
        Handle one read's worth of frames: decode them in order, publish every
        location together, then ACK them all in the order they arrived.

        Together rather than one after another, so a burst -- a tracker that
        regains signal uploads its buffered fixes at once -- reaches the sink
        as a group instead of one awaited write per fix. ACKs go out once
        every publish has returned: for BatchingSink that means queued, for a
        sink without batching it means written.
        """
        acks: list[Frame] = []
        publishes = []
        for frame in frames:
            if frame.protocol == PROTO_LOGIN:
                device_id = decode_login(frame.content)
                if device_id is None:
                    log.warning("Rejecting unreadable login packet")
                    continue
                self.device_id = device_id
                log.info("Device logged in: %s", self.device_id)
                acks.append(frame)

            elif frame.protocol in (PROTO_LOCATION, PROTO_ALARM):
                if not self.device_id:
                    log.warning("Location packet before login -- dropping frame")
                    continue
                event = decode_location(frame.content, self.device_id)
                if event is not None:
                    if frame.protocol == PROTO_ALARM:
                        event.event_type = "alarm"
                    publishes.append(self._publish(event))
                acks.append(frame)

            elif frame.protocol == PROTO_HEARTBEAT:
                log.debug("Heartbeat from %s", self.device_id)
                acks.append(frame)

            else:
                log.debug("Unhandled protocol 0x%02X from %s", frame.protocol, self.device_id)

        if publishes:
            await asyncio.gather(*publishes)
        if acks:
            for frame in acks:
                writer.write(build_ack(frame.protocol, frame.serial))
            await writer.drain()

    async def _publish(self, event: LocationEvent) -> None:
        """A sink outage must not kill the device connection."""
        try:
            await self.sink.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to publish event for %s", event.device_id)


def _log_startup_banner(server: asyncio.AbstractServer, config: Config) -> None:
    """
    Report the addresses devices should be pointed at.

    This is a raw TCP listener, not HTTP -- a browser cannot open these. The
    tcp:// form is a reminder of that: it is the host and port you put in the
    tracker's SERVER config command.
    """
    log.info("=" * 72)
    log.info("GPS ingestion gateway ready (GT06 over plain TCP)")
    for sock in server.sockets or []:
        host, port = sock.getsockname()[:2]
        log.info("  LISTEN  tcp://%s:%s", host, port)
        if host in ("0.0.0.0", "::"):
            log.info("  LOCAL   tcp://127.0.0.1:%s", port)
    log.info("-" * 72)
    log.info("  sink=%s  idle_timeout=%ss  max_buffer=%sB",
             config.sink, config.idle_timeout_s, config.max_buffer_bytes)
    if config.sink == "postgres":
        log.info("  batch_size=%s  flush_interval=%ss  queue_max=%s",
                 config.batch_size, config.flush_interval_s, config.queue_max)
    log.info("  point a device here:  SERVER,0,<this-host>,%s,0#", config.port)
    log.info("  simulate a device:    python3 tools/simulate_device.py --port %s", config.port)
    log.info("=" * 72)


async def serve(config: Config | None = None, sink: Sink | None = None) -> None:
    config = config or Config.from_env()
    sink = sink or build_sink(config)
    await sink.start()

    async def client_connected(reader, writer):
        await DeviceSession(sink, config).handle(reader, writer)

    server = await asyncio.start_server(client_connected, config.host, config.port)
    _log_startup_banner(server, config)

    try:
        async with server:
            await server.serve_forever()
    finally:
        await sink.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        log.info("Shutting down")
