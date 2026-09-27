"""
Trip reporting: distance, speed, running time and halts over a window.

The rules live here once, as plain Python, and PostgresLocationRepository's
trip_report SQL implements exactly the same ones in a single pass. The
in-memory backend calls summarize_device directly, and the test suite checks
the SQL against it, so the two cannot quietly disagree about what a halt is.

Definitions, per device, over its fixes in the window ordered by
(received_at, id), with a missing speed read as 0 like the dashboard does:

- distance: the sum of great-circle distances between consecutive fixes, the
  same haversine the dashboard's trackDistanceKm uses.
- max speed: the fastest reported speed_kmh.
- running time: for each consecutive pair whose earlier fix was moving, the
  time between them -- unless that gap exceeds MAX_RUNNING_GAP, which means
  the tracker went quiet (tunnel, flat battery), not that it drove.
- a stop: a run of at least two consecutive stationary fixes, lasting from
  the first to the last of them -- device-detail.ts's `stops`.
- a halt: a stop of HALT_THRESHOLD or longer, the threshold the live map's
  "Halted" badge uses. Shorter stops (traffic lights) are not reported.

A stop cut by either end of the window counts only its part inside it.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

HALT_THRESHOLD = timedelta(minutes=10)
MAX_RUNNING_GAP = timedelta(minutes=10)

_EARTH_RADIUS_M = 6_371_000


@dataclass(frozen=True)
class ReportFix:
    latitude: float
    longitude: float
    speed_kmh: int | None
    received_at: datetime


@dataclass(frozen=True)
class DeviceTrips:
    distance_km: float
    max_speed_kmh: int | None
    running_minutes: float
    halt_count: int
    halt_minutes: float
    longest_halt_minutes: float
    fix_count: int
    first_fix_at: datetime | None
    last_fix_at: datetime | None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rad = math.radians
    d_lat = rad(lat2 - lat1)
    d_lon = rad(lon2 - lon1)
    h = math.sin(d_lat / 2) ** 2 + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def summarize_device(fixes: list[ReportFix]) -> DeviceTrips:
    """`fixes` must already be in (received_at, id) order."""
    metres = 0.0
    running = timedelta()
    halts: list[timedelta] = []
    stop_start: datetime | None = None
    stop_end: datetime | None = None
    stop_len = 0

    def close_stop() -> None:
        if stop_len >= 2 and stop_start is not None and stop_end is not None:
            length = stop_end - stop_start
            if length >= HALT_THRESHOLD:
                halts.append(length)

    previous: ReportFix | None = None
    for fix in fixes:
        if previous is not None:
            metres += haversine_m(previous.latitude, previous.longitude, fix.latitude, fix.longitude)
            gap = fix.received_at - previous.received_at
            if (previous.speed_kmh or 0) > 0 and gap <= MAX_RUNNING_GAP:
                running += gap
        if (fix.speed_kmh or 0) == 0:
            if stop_len == 0:
                stop_start = fix.received_at
            stop_end = fix.received_at
            stop_len += 1
        else:
            close_stop()
            stop_len = 0
        previous = fix
    close_stop()

    speeds = [f.speed_kmh for f in fixes if f.speed_kmh is not None]
    return DeviceTrips(
        distance_km=metres / 1000,
        max_speed_kmh=max(speeds) if speeds else None,
        running_minutes=running.total_seconds() / 60,
        halt_count=len(halts),
        halt_minutes=sum(h.total_seconds() for h in halts) / 60,
        longest_halt_minutes=max((h.total_seconds() for h in halts), default=0) / 60,
        fix_count=len(fixes),
        first_fix_at=fixes[0].received_at if fixes else None,
        last_fix_at=fixes[-1].received_at if fixes else None,
    )
