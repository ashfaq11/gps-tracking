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
