"""Decoding of GT06 packet payloads and encoding of server ACKs."""

import logging
import struct
from datetime import datetime, timezone

from ..models import CellReport, CommandReply, LocationEvent, StatusEvent
from .constants import (
    PROTO_ALARM,
    PROTO_COMMAND,
    PROTO_COMMAND_REPLY_NEW,
    PROTO_LOCATION,
    PROTO_LOCATION_ACC,
    START_SHORT,
    STOP_BITS,
)
from .crc import crc16_itu

log = logging.getLogger(__name__)

# Fixed-point scale: raw values are minutes * 30000.
_COORD_SCALE = 30000.0 * 60.0

# Bits in the two-byte course/status word that closes a location payload.
_COURSE_MASK = 0x03FF
_BIT_NORTH = 0x0400  # set => northern hemisphere
_BIT_WEST = 0x0800  # set => western hemisphere
_BIT_GPS_FIXED = 0x1000  # set => GPS has a real fix

LOCATION_MIN_LEN = 18

# MCC(2) MNC(1) LAC(2) CellID(3); one byte longer when MNC takes two bytes.
_CELL_LEN = 8
# Set on the MCC word by newer Concox firmware to announce a two-byte MNC.
# A real MCC never exceeds 999, so the bit cannot be mistaken for data.
_BIT_MNC_2_BYTES = 0x8000

# LBS-only content: 6-byte timestamp, then the serving cell.
LBS_MIN_LEN = 6 + _CELL_LEN

# Terminal information byte, sent first in a heartbeat and after the cell in
# an alarm. Bit 1 mirrors the ACC (ignition) wire.
_BIT_ACC_ON = 0x02


def bcd_to_int(byte_val: int) -> int:
    """GT06 packs some fields as binary-coded decimal (each nibble = a digit)."""
    return (byte_val >> 4) * 10 + (byte_val & 0x0F)


def decode_login(content: bytes) -> str | None:
    """
    Login content starts with an 8-byte terminal ID holding a 15-digit IMEI
    in BCD, left-padded with a single zero nibble.
    """
    if len(content) < 8:
        log.warning("Login content too short (%d bytes)", len(content))
        return None
    digits = "".join(f"{b:02X}" for b in content[:8])
    if not digits.isdigit():
        log.warning("Login terminal ID is not BCD: %s", digits)
        return None
    # Exactly one pad nibble -- do not strip further, an IMEI may legitimately
    # continue with a zero digit.
    return digits[1:] if len(digits) == 16 and digits[0] == "0" else digits


def _decode_timestamp(content: bytes) -> datetime | None:
    """Bytes 0-5 are YY MM DD HH MM SS as raw binary, in UTC."""
    yy, mm, dd, hh, mi, ss = content[0:6]
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss, tzinfo=timezone.utc)
    except ValueError:
        # Devices report zeroed or garbage dates before their clock syncs.
        log.debug("Unparseable device timestamp: %s", content[0:6].hex())
        return None


def _decode_cell(data: bytes) -> tuple[tuple[int, int, int, int] | None, int]:
    """
    Decode MCC, MNC, LAC and CellID from the start of `data`.

    Returns ((mcc, mnc, lac, cell_id), bytes consumed). The cell is None if
    the block is truncated (consumed 0) or all-zero -- which is what a tracker
    with no GSM registration sends, and is no more a cell than an unset clock
    is a date.
    """
    if len(data) < 2:
        return None, 0
    mcc = struct.unpack(">H", data[0:2])[0]
    mnc_len = 2 if mcc & _BIT_MNC_2_BYTES else 1
    mcc &= ~_BIT_MNC_2_BYTES & 0xFFFF
    consumed = _CELL_LEN - 1 + mnc_len
    if len(data) < consumed:
        return None, 0
    mnc = int.from_bytes(data[2 : 2 + mnc_len], "big")
    lac = struct.unpack(">H", data[2 + mnc_len : 4 + mnc_len])[0]
    cell_id = int.from_bytes(data[4 + mnc_len : consumed], "big")
    if mcc == 0 and lac == 0 and cell_id == 0:
        return None, consumed
    return (mcc, mnc, lac, cell_id), consumed


def _ignition(terminal_info: int) -> bool:
    return bool(terminal_info & _BIT_ACC_ON)


def decode_heartbeat(content: bytes, device_id: str) -> StatusEvent | None:
    """
    Heartbeat payload (protocol 0x13):
      [1B terminal info][1B voltage level][1B GSM signal][2B alarm + language]

    Only the terminal info byte matters here. An empty heartbeat (some
    clones send one) says nothing about ignition, so it yields None.
    """
    if not content:
        return None
    return StatusEvent(device_id=device_id, ignition=_ignition(content[0]))


def decode_location(
    content: bytes, device_id: str, protocol: int = PROTO_LOCATION
) -> LocationEvent | None:
    """
    Location, alarm and ACC-location packets share their first 18 bytes:
      [6B date/time][1B gps info + sat count][4B lat][4B lon][1B speed][2B course+flags]

    What follows depends on `protocol`, and is decoded when present:
      0x12  [cell]
      0x22  [cell][1B ACC][1B upload reason][1B re-upload flag][4B mileage, optional]
      0x16  [1B LBS length][cell][1B terminal info][1B voltage][1B GSM][2B alarm]
    where [cell] is [2B MCC][1B MNC][2B LAC][3B CellID]. The alarm's LBS
    length counts itself (9 for a plain cell, 0 for none), and its terminal
    info byte carries ignition in bit 1.
    """
    if len(content) < LOCATION_MIN_LEN:
        log.warning("Location content too short (%d bytes) for %s", len(content), device_id)
        return None

    fixed_at = _decode_timestamp(content)

    # High nibble is the length of the GPS info block, low nibble the number
    # of satellites in view. Fix validity lives in the course/flags word.
    satellites = content[6] & 0x0F

    lat_raw, lon_raw = struct.unpack(">II", content[7:15])
    speed = content[15]
    flags = struct.unpack(">H", content[16:18])[0]

    latitude = lat_raw / _COORD_SCALE
    longitude = lon_raw / _COORD_SCALE
    if not flags & _BIT_NORTH:
        latitude = -latitude
    if flags & _BIT_WEST:
        longitude = -longitude

    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        log.warning(
            "Out-of-range coordinates from %s: lat=%.6f lon=%.6f", device_id, latitude, longitude
        )
        return None

    cell, ignition = None, None
    if protocol != PROTO_ALARM:
        tail = content[LOCATION_MIN_LEN:]
        cell, consumed = _decode_cell(tail)
        # `consumed` is 0 only for a truncated cell, and then the offset of
        # the ACC byte is unknown -- guessing would store noise as ignition.
        if protocol == PROTO_LOCATION_ACC and consumed and len(tail) > consumed:
            ignition = tail[consumed] != 0
    elif len(content) > LOCATION_MIN_LEN:
        lbs_len = content[LOCATION_MIN_LEN]
        if lbs_len:
            cell, _ = _decode_cell(content[LOCATION_MIN_LEN + 1 : LOCATION_MIN_LEN + lbs_len])
        status_at = LOCATION_MIN_LEN + max(lbs_len, 1)
        if len(content) > status_at:
            ignition = _ignition(content[status_at])
    mcc, mnc, lac, cell_id = cell or (None, None, None, None)

    return LocationEvent(
        device_id=device_id,
        latitude=round(latitude, 6),
        longitude=round(longitude, 6),
        speed_kmh=speed,
        course_deg=flags & _COURSE_MASK,
        gps_fixed=bool(flags & _BIT_GPS_FIXED),
        satellites=satellites,
        fixed_at=fixed_at,
        mcc=mcc,
        mnc=mnc,
        lac=lac,
        cell_id=cell_id,
        ignition=ignition,
    )


def decode_lbs(content: bytes, device_id: str) -> CellReport | None:
    """
    LBS-only payload (protocol 0x18), sent when the tracker has no GPS fix:
      [6B date/time][2B MCC][1B MNC][2B LAC][3B CellID][1B signal][neighbour cells..]

    Only the serving cell and its signal are decoded; the neighbour cells
    that follow matter only to a triangulating geolocation service.
    """
    if len(content) < LBS_MIN_LEN:
        log.warning("LBS content too short (%d bytes) for %s", len(content), device_id)
        return None
    cell, consumed = _decode_cell(content[6:])
    if cell is None:
        log.debug("LBS packet from %s carries no cell", device_id)
        return None
    mcc, mnc, lac, cell_id = cell
    signal_at = 6 + consumed
    return CellReport(
        device_id=device_id,
        mcc=mcc,
        mnc=mnc,
        lac=lac,
        cell_id=cell_id,
        signal=content[signal_at] if len(content) > signal_at else None,
        reported_at=_decode_timestamp(content),
    )


def build_frame(protocol_number: int, serial: int, content: bytes = b"") -> bytes:
    """
    Assemble a complete short (78 78) GT06 frame with a valid CRC.

    Used for server ACKs, and by the device simulator to produce packets that
    are byte-identical to what real hardware sends.
    """
    body = bytes([protocol_number]) + content + struct.pack(">H", serial)
    length = len(body) + 2  # + the CRC that follows
    if length > 0xFF:
        raise ValueError("Content too long for a short frame")
    checksum = crc16_itu(bytes([length]) + body)
    return START_SHORT + bytes([length]) + body + struct.pack(">H", checksum) + STOP_BITS


def build_ack(protocol_number: int, serial: int, content: bytes = b"") -> bytes:
    """
    Build the server response a device waits for before it considers a packet
    delivered. The echoed serial number is what pairs the ACK to the packet,
    and the CRC is checked by real Concox hardware.
    """
    return build_frame(protocol_number, serial, content)


# Language field closing a command: 0x0002 asks for English replies.
_LANGUAGE_ENGLISH = 0x0002
# Reply text encodings in a 0x21 reply.
_ENCODING_UTF16 = 0x02


def build_command(serial: int, server_flag: int, command: str) -> bytes:
    """
    A server command frame (protocol 0x80):
      [1B length][4B server flag][command ASCII][2B language]
    where the length counts the flag and the command. The tracker runs the
    command as if it had arrived by SMS -- the PT06 manual's "RELAY,1#" --
    and answers with a 0x15/0x21 reply carrying the same server flag.
    """
    text = command.encode("ascii")
    info_len = 4 + len(text)
    if info_len > 0xFF:
        raise ValueError("Command too long")
    content = (
        bytes([info_len])
        + struct.pack(">I", server_flag)
        + text
        + struct.pack(">H", _LANGUAGE_ENGLISH)
    )
    return build_frame(PROTO_COMMAND, serial, content)


def decode_command_reply(protocol: int, content: bytes) -> CommandReply | None:
    """
    The tracker's answer to a command:
      0x15  [1B length][4B server flag][reply text][2B language, optional]
      0x21  [4B server flag][1B encoding: 1 ASCII, 2 UTF-16BE][reply text]
    The 0x15 length counts the flag and the text, which is how the optional
    language suffix is told apart from the text.
    """
    if protocol == PROTO_COMMAND_REPLY_NEW:
        if len(content) < 5:
            return None
        flag = struct.unpack(">I", content[0:4])[0]
        raw = content[5:]
        codec = "utf-16-be" if content[4] == _ENCODING_UTF16 else "ascii"
    else:
        if len(content) < 5:
            return None
        info_len = content[0]
        flag = struct.unpack(">I", content[1:5])[0]
        raw = content[5 : 1 + info_len] if info_len >= 4 else content[5:]
        codec = "ascii"
    text = raw.decode(codec, errors="replace").strip("\x00").strip()
    return CommandReply(server_flag=flag, text=text)
