"""Turning a TCP byte stream into discrete GT06 frames."""

import logging
import struct
from dataclasses import dataclass

from .constants import START_LONG, START_SHORT, STOP_BITS, TRAILER_LEN

from .crc import crc16_itu

log = logging.getLogger(__name__)

_STARTS = (START_SHORT, START_LONG)
_START_FIRST_BYTES = (b"\x78", b"\x79")


@dataclass(frozen=True)
class Frame:
    protocol: int
    content: bytes
    serial: int
    crc: int
    crc_ok: bool


class FrameDecoder:
    """
    Accumulates bytes off the wire and hands back complete frames.

    Devices do not align frames to TCP reads: one read may carry half a frame
    or three and a half, so the leftover has to survive between reads.
    """

    def __init__(self, max_buffer_bytes: int = 8192):
        self._buffer = b""
        self._max_buffer_bytes = max_buffer_bytes
        self.dropped_frames = 0

    @property
    def buffer(self) -> bytes:
        return self._buffer

    def feed(self, data: bytes) -> list[Frame]:
        """
        Add freshly read bytes and return every complete, valid frame now
        available. Corrupt frames are counted in `dropped_frames` and skipped.
        """
        self._buffer += data
        frames: list[Frame] = []
        while True:
            frame, consumed = self._next_frame()
            if not consumed:
                break
            if frame is not None:
                frames.append(frame)
        self._guard_buffer()
        return frames

    def _guard_buffer(self) -> None:
        """A peer that never sends a start marker must not grow the buffer."""
        if len(self._buffer) > self._max_buffer_bytes:
            log.warning(
                "Read buffer exceeded %d bytes with no complete frame; discarding",
                self._max_buffer_bytes,
            )
            self._buffer = b""

    def _next_frame(self) -> tuple[Frame | None, bool]:
        """Return (frame, made_progress). `frame` is None for a bad frame."""
        self._resync()
        buf = self._buffer
        header = buf[:2]
        if header not in _STARTS:
            return None, False

        len_size = 1 if header == START_SHORT else 2
        head_len = 2 + len_size
        if len(buf) < head_len:
            return None, False

        length = buf[2] if len_size == 1 else struct.unpack(">H", buf[2:4])[0]

        # `length` counts protocol + content + serial + crc.
        if length < 1 + TRAILER_LEN:
            log.warning("Implausible frame length %d; resyncing", length)
            self._buffer = buf[2:]
            self.dropped_frames += 1
            return None, True

        frame_len = head_len + length + len(STOP_BITS)
        if len(buf) < frame_len:
            return None, False  # wait for the rest of the frame

        raw = buf[:frame_len]
        if raw[-2:] != STOP_BITS:
            # Length byte was garbage or we locked onto a false start marker.
            log.warning("Frame missing stop bytes; resyncing past start marker")
            self._buffer = buf[2:]
            self.dropped_frames += 1
            return None, True

        self._buffer = buf[frame_len:]

        body = raw[2 : head_len + length - 2]  # length field through serial
        protocol = raw[head_len]
        content = raw[head_len + 1 : frame_len - 2 - TRAILER_LEN]
        serial, crc = struct.unpack(">HH", raw[frame_len - 2 - TRAILER_LEN : frame_len - 2])

        if crc16_itu(body) != crc:
            log.warning("CRC mismatch on protocol 0x%02X frame; dropping", protocol)
            self.dropped_frames += 1
            return None, True

        return Frame(protocol=protocol, content=content, serial=serial, crc=crc, crc_ok=True), True

    def _resync(self) -> None:
        """
        Drop bytes ahead of the first start marker, but keep a trailing 0x78 /
        0x79 that may be the first half of a marker split across two reads.
        """
        buf = self._buffer
        found = [i for i in (buf.find(s) for s in _STARTS) if i != -1]
        if found:
            self._buffer = buf[min(found) :]
        elif buf[-1:] in _START_FIRST_BYTES:
            self._buffer = buf[-1:]
        else:
            self._buffer = b""
