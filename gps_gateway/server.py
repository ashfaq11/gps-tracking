"""Asyncio TCP server: one persistent connection per device."""

import asyncio
import logging
from typing import Optional

from .commands import (
    CommandQueue,
    Deliverable,
    SessionRegistry,
    build_command_queue,
    reply_is_failure,
)
from .config import Config
from .logging_setup import configure_logging
from .models import LocationEvent, StatusEvent
from .protocol import (
    PROTO_ALARM,
    PROTO_COMMAND_REPLY,
    PROTO_COMMAND_REPLY_NEW,
    PROTO_HEARTBEAT,
    PROTO_LBS,
    PROTO_LOCATION,
    PROTO_LOCATION_ACC,
    PROTO_LOGIN,
    build_ack,
    build_command,
    decode_command_reply,
    decode_heartbeat,
    decode_lbs,
    decode_location,
    decode_login,
)
from .protocol.framing import Frame, FrameDecoder
from .sinks import Sink, build_sink

log = logging.getLogger(__name__)

_READ_SIZE = 4096


class DeviceSession(Deliverable):
    """
    Per-connection state. A tracker logs in once, then streams location and
    heartbeat frames over the same socket until it loses signal. The same
    socket carries commands the other way (see gps_gateway/commands.py).
    """

    def __init__(
        self,
        sink: Sink,
        config: Config,
        commands: CommandQueue | None = None,
        registry: SessionRegistry | None = None,
    ):
        self.sink = sink
        self.config = config
        self.commands = commands or CommandQueue()
        self.registry = registry
        self.device_id: Optional[str] = None
        self.decoder = FrameDecoder(max_buffer_bytes=config.max_buffer_bytes)
        self.writer: asyncio.StreamWriter | None = None
        # Ignition as this tracker last reported it on this connection -- a
        # fresher view than device_status for the last check before a cut.
        self.ignition: bool | None = None
        self._out_serial = 0
        # Command id -> timer that records 'no_reply' if no answer comes.
        self._awaiting: dict[int, asyncio.TimerHandle] = {}
        self._delivery_lock = asyncio.Lock()
        self._delivery_tasks: set[asyncio.Task] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
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
            if self.registry is not None and self.device_id:
                self.registry.remove(self.device_id, self)
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
        self.writer = writer
        acks: list[Frame] = []
        publishes = []
        logged_in = False
        for frame in frames:
            if frame.protocol == PROTO_LOGIN:
                device_id = decode_login(frame.content)
                if device_id is None:
                    log.warning("Rejecting unreadable login packet")
                    continue
                if self.registry is not None and self.device_id and self.device_id != device_id:
                    self.registry.remove(self.device_id, self)
                self.device_id = device_id
                log.info("Device logged in: %s", self.device_id)
                if self.registry is not None:
                    self.registry.add(device_id, self)
                logged_in = True
                acks.append(frame)

            elif frame.protocol in (PROTO_LOCATION, PROTO_LOCATION_ACC, PROTO_ALARM):
                if not self.device_id:
                    log.warning("Location packet before login -- dropping frame")
                    continue
                event = decode_location(frame.content, self.device_id, frame.protocol)
                if event is not None:
                    if frame.protocol == PROTO_ALARM:
                        event.event_type = "alarm"
                    if event.ignition is not None:
                        self.ignition = event.ignition
                    publishes.append(self._publish(event))
                acks.append(frame)

            elif frame.protocol == PROTO_LBS:
                if not self.device_id:
                    log.warning("LBS packet before login -- dropping frame")
                    continue
                # No coordinates, so nothing for device_locations: logged for
                # now, and ACKed so the device stops resending it.
                report = decode_lbs(frame.content, self.device_id)
                if report is not None:
                    log.info(
                        "LBS from %s: mcc=%d mnc=%d lac=%d cell_id=%d signal=%s",
                        report.device_id,
                        report.mcc,
                        report.mnc,
                        report.lac,
                        report.cell_id,
                        report.signal,
                    )
                acks.append(frame)

            elif frame.protocol == PROTO_HEARTBEAT:
                log.debug("Heartbeat from %s", self.device_id)
                status = decode_heartbeat(frame.content, self.device_id) if self.device_id else None
                if status is not None:
                    self.ignition = status.ignition
                    publishes.append(self._publish_status(status))
                acks.append(frame)

            elif frame.protocol in (PROTO_COMMAND_REPLY, PROTO_COMMAND_REPLY_NEW):
                if self.device_id:
                    publishes.append(self._command_replied(frame))

            else:
                log.debug("Unhandled protocol 0x%02X from %s", frame.protocol, self.device_id)

        if publishes:
            await asyncio.gather(*publishes)
        if acks:
            for frame in acks:
                writer.write(build_ack(frame.protocol, frame.serial))
            await writer.drain()
        if logged_in:
            # Anything queued while this tracker was offline.
            self.schedule_delivery()

    # --- commands -------------------------------------------------------

    def schedule_delivery(self) -> None:
        """Check for commands in the background; the reader loop never waits."""
        if not self.device_id or self.writer is None:
            return
        task = asyncio.create_task(self.deliver_commands())
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)

    async def deliver_commands(self) -> None:
        """
        Claim this tracker's commands and send them. The database already
        refused any cut while device_status says the ignition is not off; a
        heartbeat on this very connection saying it is on refuses it too.
        """
        async with self._delivery_lock:
            device_id, writer = self.device_id, self.writer
            if not device_id or writer is None:
                return
            try:
                pending = await self.commands.claim(device_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not claim commands for %s", device_id)
                return
            for command in pending:
                if command.action == "cut" and self.ignition:
                    log.warning("Not cutting %s: ignition is on (command %d)", device_id, command.id)
                    await self._complete(command.id, ok=False, error="ignition_on")
                    continue
                self._out_serial = (self._out_serial + 1) & 0xFFFF
                try:
                    writer.write(build_command(self._out_serial, command.id, command.command))
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    log.warning("Connection lost sending command %d to %s", command.id, device_id)
                    await self._complete(command.id, ok=False, error="connection_lost")
                    continue
                log.info("Sent %r to %s (command %d)", command.command, device_id, command.id)
                loop = asyncio.get_running_loop()
                self._awaiting[command.id] = loop.call_later(
                    self.config.command_reply_timeout_s, self._reply_timed_out, command.id
                )

    def _reply_timed_out(self, command_id: int) -> None:
        if self._awaiting.pop(command_id, None) is not None:
            log.warning("No reply from %s to command %d", self.device_id, command_id)
            task = asyncio.create_task(self._complete(command_id, ok=False, error="no_reply"))
            self._delivery_tasks.add(task)
            task.add_done_callback(self._delivery_tasks.discard)

    async def _command_replied(self, frame: Frame) -> None:
        reply = decode_command_reply(frame.protocol, frame.content)
        if reply is None:
            log.warning("Unreadable command reply from %s", self.device_id)
            return
        timer = self._awaiting.pop(reply.server_flag, None)
        if timer is not None:
            timer.cancel()
        failed = reply_is_failure(reply.text)
        log.info(
            "Reply from %s to command %d: %r", self.device_id, reply.server_flag, reply.text
        )
        # Completed even when not awaited here (a late answer after
        # 'no_reply'): the database only accepts it for this device's own
        # command, still waiting on an answer.
        await self._complete(
            reply.server_flag,
            ok=not failed,
            reply=reply.text,
            error="tracker_reported_failure" if failed else None,
        )

    async def _complete(self, command_id: int, *, ok: bool, reply=None, error=None) -> None:
        try:
            await self.commands.complete(
                command_id, self.device_id, ok=ok, reply=reply, error=error
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not record the outcome of command %d", command_id)

    async def _publish(self, event: LocationEvent) -> None:
        """A sink outage must not kill the device connection."""
        try:
            await self.sink.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to publish event for %s", event.device_id)

    async def _publish_status(self, status: StatusEvent) -> None:
        try:
            await self.sink.publish_status(status)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to record ignition for %s", status.device_id)

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


async def serve(
    config: Config | None = None,
    sink: Sink | None = None,
    commands: CommandQueue | None = None,
) -> None:
    config = config or Config.from_env()
    sink = sink or build_sink(config)
    commands = commands or build_command_queue(
        config.sink, config.pg_dsn, config.pg_statement_cache_size
    )
    registry = SessionRegistry()
    await sink.start()
    await commands.start(registry.notify, registry.notify_all)

    async def client_connected(reader, writer):
        await DeviceSession(sink, config, commands, registry).handle(reader, writer)

    server = await asyncio.start_server(client_connected, config.host, config.port)
    _log_startup_banner(server, config)

    try:
        async with server:
            await server.serve_forever()
    finally:
        await commands.stop()
        await sink.stop()


def main() -> None:
    config = Config.from_env()
    configure_logging(config)
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        log.info("Shutting down")
