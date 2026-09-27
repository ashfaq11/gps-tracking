"""Normalized events emitted downstream."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LocationEvent:
    device_id: str
    latitude: float
    longitude: float
    speed_kmh: int
    course_deg: int
    gps_fixed: bool
    satellites: int = 0
    # When the device says the fix was taken. None if the device clock was
    # unset or the field was unparseable; consumers should fall back to
    # received_at in that case.
    fixed_at: datetime | None = None
    # When this gateway decoded the packet.
    received_at: datetime = field(default_factory=_utcnow)
    event_type: str = "location"
    # Serving GSM/LTE cell the tracker was camped on, when the packet carries
    # it. All four are None together if it did not (or reported all zeros).
    mcc: int | None = None
    mnc: int | None = None
    lac: int | None = None
    cell_id: int | None = None


@dataclass
class CellReport:
    """
    An LBS-only packet (protocol 0x18): the serving cell with no GPS
    position. Not a LocationEvent -- there are no coordinates to store until
    the cell is resolved to one through a geolocation service.
    """

    device_id: str
    mcc: int
    mnc: int
    lac: int
    cell_id: int
    # Raw signal strength byte, higher is stronger; None if not sent.
    signal: int | None = None
    reported_at: datetime | None = None
