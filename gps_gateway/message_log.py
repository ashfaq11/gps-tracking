"""
One human-readable line per GT06 packet, in and out.

Everything a tracker sends -- login, location, heartbeat, alarm, cell-only
(LBS) position, command reply, and anything this gateway does not handle --
is decoded again here purely for reading, and written to its own logger,
`gps_gateway.messages` (the `gateway-messages.log` file; see
logging_setup.py). The server's own decoding in protocol/codec.py stays the
single source of truth for what gets stored; nothing here feeds the database.

    2026-09-30 11:25:32 IST IN   868720065896205  LOCATION   #0042  fixed 2026-09-30
        11:25:30 IST · 12.971234, 77.594321 · 44 km/h · heading 267° W · 9 satellites ·
        GPS lock · ignition ON · cell 404/45/1234/56789 · map https://maps.google.com/?q=...

Times are shown in IST (India has no daylight saving, so a fixed +05:30 is
exact and needs no tz database in the container).
"""

import logging
from datetime import datetime, timedelta, timezone

from .protocol.codec import decode_command_reply, decode_lbs, decode_location, decode_login
from .protocol.constants import (
    PROTO_ALARM,
    PROTO_COMMAND,
    PROTO_COMMAND_REPLY,
    PROTO_COMMAND_REPLY_NEW,
    PROTO_HEARTBEAT,
    PROTO_LBS,
    PROTO_LOCATION,
    PROTO_LOCATION_ACC,
    PROTO_LOGIN,
)
from .protocol.framing import Frame

log = logging.getLogger("gps_gateway.messages")

IST = timezone(timedelta(hours=5, minutes=30), "IST")

PROTOCOL_NAMES = {
    PROTO_LOGIN: "LOGIN",
    PROTO_LOCATION: "LOCATION",
    PROTO_LOCATION_ACC: "LOCATION",
    PROTO_HEARTBEAT: "HEARTBEAT",
    PROTO_ALARM: "ALARM",
    PROTO_LBS: "CELL-ONLY",
    PROTO_COMMAND: "COMMAND",
    PROTO_COMMAND_REPLY: "REPLY",
    PROTO_COMMAND_REPLY_NEW: "REPLY",
}

# GT06 heartbeat/alarm status bytes, per the protocol document.
_VOLTAGE = ["no power", "extremely low", "very low", "low", "medium", "high", "very high"]
_GSM = ["no signal", "extremely weak", "weak", "good", "strong"]
# Bits 3-5 of the terminal info byte.
_TERMINAL_ALARM = {1: "shock", 2: "power cut", 3: "low battery", 4: "SOS"}
# The alarm byte closing a heartbeat or an alarm packet.
_ALARM = {
    0x00: None,
    0x01: "SOS",
    0x02: "power cut",
    0x03: "vibration",
    0x04: "entered fence",
    0x05: "left fence",
    0x06: "overspeed",
    0x09: "moved",
    0x0E: "low external battery",
    0x0F: "low backup battery",
    0x13: "tamper",
}
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _ist(moment: datetime | None) -> str:
    if moment is None:
        return "unknown time"
    return moment.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _level(names: list[str], value: int) -> str:
    return f"{names[value]} ({value}/{len(names) - 1})" if 0 <= value < len(names) else f"{value}"


def _terminal(info: int) -> list[str]:
    parts = [
        f"ignition {'ON' if info & 0x02 else 'OFF'}",
        "GPS tracking on" if info & 0x40 else "GPS tracking off",
    ]
    if info & 0x04:
        parts.append("charging")
    if info & 0x80:
        parts.append("fuel/relay CUT")
    if info & 0x01:
        parts.append("armed")
    alarm = _TERMINAL_ALARM.get((info >> 3) & 0x07)
    if alarm:
        parts.append(f"alarm: {alarm}")
    return parts


def _status_tail(data: bytes) -> list[str]:
    """[terminal info][voltage][GSM][alarm][language] -- as much as is present."""
    parts: list[str] = []
    if len(data) >= 1:
        parts += _terminal(data[0])
    if len(data) >= 2:
        parts.append(f"battery {_level(_VOLTAGE, data[1])}")
    if len(data) >= 3:
        parts.append(f"GSM {_level(_GSM, data[2])}")
    if len(data) >= 4:
        alarm = _ALARM.get(data[3], f"code 0x{data[3]:02X}")
        if alarm:
            parts.append(f"ALARM: {alarm}")
    return parts


def _location(frame: Frame, device_id: str) -> list[str]:
    event = decode_location(frame.content, device_id or "?", frame.protocol)
    if event is None:
        return [f"unreadable location ({len(frame.content)} bytes)"]
    heading = _COMPASS[round(event.course_deg / 45) % 8]
    parts = [
        f"fixed {_ist(event.fixed_at)}",
        f"{event.latitude:.6f}, {event.longitude:.6f}",
        f"{event.speed_kmh} km/h",
        f"heading {event.course_deg}° {heading}",
        f"{event.satellites} satellites",
        "GPS lock" if event.gps_fixed else "NO GPS LOCK (not used for routes)",
    ]
    if event.ignition is not None:
        parts.append(f"ignition {'ON' if event.ignition else 'OFF'}")
    if event.mcc is not None:
        parts.append(f"cell {event.mcc}/{event.mnc}/{event.lac}/{event.cell_id}")
    if frame.protocol == PROTO_ALARM and len(frame.content) > 18:
        lbs_len = frame.content[18]
        parts += _status_tail(frame.content[18 + max(lbs_len, 1) :])
    parts.append(f"map https://maps.google.com/?q={event.latitude:.6f},{event.longitude:.6f}")
    return parts


def describe(frame: Frame, device_id: str | None) -> str:
    """The human-readable body for one inbound frame."""
    content = frame.content
    if frame.protocol == PROTO_LOGIN:
        imei = decode_login(content)
        return f"IMEI {imei}" if imei else f"unreadable login ({content.hex()})"
    if frame.protocol in (PROTO_LOCATION, PROTO_LOCATION_ACC, PROTO_ALARM):
        return " · ".join(_location(frame, device_id or ""))
    if frame.protocol == PROTO_HEARTBEAT:
        return " · ".join(_status_tail(content)) if content else "empty heartbeat"
    if frame.protocol == PROTO_LBS:
        report = decode_lbs(content, device_id or "?")
        if report is None:
            return f"no usable cell ({len(content)} bytes)"
        return (
            f"no GPS, cell tower only · {_ist(report.reported_at)} · "
            f"cell {report.mcc}/{report.mnc}/{report.lac}/{report.cell_id} · "
            f"signal {report.signal if report.signal is not None else 'unknown'}"
        )
    if frame.protocol in (PROTO_COMMAND_REPLY, PROTO_COMMAND_REPLY_NEW):
        reply = decode_command_reply(frame.protocol, content)
        if reply is None:
            return f"unreadable reply ({content.hex()})"
        return f"to command {reply.server_flag}: {reply.text!r}"
    return f"not handled by this gateway · {len(content)} bytes · {content.hex()}"


def log_inbound(frame: Frame, device_id: str | None, peer: str, raw: bool = False) -> None:
    """Write one IN line. Never raises: a logging bug must not drop a fix."""
    if not log.isEnabledFor(logging.INFO):
        return
    try:
        name = PROTOCOL_NAMES.get(frame.protocol, f"0x{frame.protocol:02X}")
        body = describe(frame, device_id)
        line = f"IN   {device_id or peer:<16} {name:<10} #{frame.serial:04d}  {body}"
        if not frame.crc_ok:
            line += " · BAD CRC"
        if raw:
            line += f" · raw {frame.protocol:02x}{frame.content.hex()}"
        log.info(line)
    except Exception:  # pragma: no cover - defensive
        logging.getLogger(__name__).exception("Could not describe a frame")


def log_event(direction: str, who: str, what: str, detail: str) -> None:
    """Lines that aren't a decoded packet: connects, refusals, commands sent."""
    log.info(f"{direction:<4} {who:<16} {what:<10}        {detail}")
