"""Decoding of GT06 packet payloads and encoding of server ACKs."""

import logging
import struct
from datetime import datetime, timezone

from ..models import LocationEvent
from .constants import START_SHORT, STOP_BITS
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


def decode_location(content: bytes, device_id: str) -> LocationEvent | None:
    """
    Location payload layout (first 18 bytes; some devices append extras):
      [6B date/time][1B gps info + sat count][4B lat][4B lon][1B speed][2B course+flags]
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

    return LocationEvent(
        device_id=device_id,
        latitude=round(latitude, 6),
        longitude=round(longitude, 6),
        speed_kmh=speed,
        course_deg=flags & _COURSE_MASK,
        gps_fixed=bool(flags & _BIT_GPS_FIXED),
        satellites=satellites,
        fixed_at=fixed_at,
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
