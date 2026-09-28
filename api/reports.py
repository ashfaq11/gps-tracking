"""
Trip reporting: distance, speed, running time and halts over a window.

The rules live here once, as plain Python, and PostgresLocationRepository's
trip_report SQL implements exactly the same ones in a single pass. The
in-memory backend calls summarize_device directly, and the test suite checks
the SQL against it, so the two cannot quietly disagree about what a halt is.

Definitions, per device, over its fixes in the window ordered by (fix
time, id), with a missing speed read as 0 like the dashboard does. A fix's
time is when the tracker took it (`fixed_at`), not when it reached us --
see `fix_time`.

The positions counted (`fix_count`, first/last fix) are every fix in the
window. Everything else is measured over the *route* -- `clean_route`,
the same filtering the dashboard's map applies (core/format.ts cleanRoute):

- no GPS lock (gps_fixed false -- the gateway also stores a GT06 fix from
  0 satellites that way) and 0,0 positions are left out
- a dirty point is left out: a fix farther from both neighbours than the
  speeds reported at either end of each leg allow (average, x ROUTE_SPEED_SLACK,
  + ROUTE_JITTER_M), while those neighbours agree with each other; fixes
  more than ROUTE_SPIKE_MAX_GAP apart are not judged. Neighbours are the
  adjacent usable fixes -- what one window-function pass in SQL can see.

- distance: the sum of great-circle distances between consecutive fixes, the
  same haversine the dashboard's trackDistanceKm uses.
- max speed: the fastest *plausible* speed_kmh. A reading is ignored when
  the fix had no GPS lock (gps_fixed false: the speed field is then
  meaningless), when it exceeds MAX_PLAUSIBLE_SPEED_KMH (GT06 sends speed as
  one byte, and junk arrives as 240-255), or when it is an isolated spike --
  more than SPIKE_KMH above both neighbouring readings, since a real vehicle
  ramps up rather than jumping 40 -> 240 -> 40. The dashboard's own top-speed
  figures (core/format.ts plausibleTopSpeed) apply the same three rules.
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
# fix_time's sanity bounds on the tracker's own clock -- the same ones
# check_geofences() in sql/schema.sql applies to a crossing's time.
CLOCK_AHEAD_LIMIT = timedelta(minutes=10)
CLOCK_BEHIND_LIMIT = timedelta(days=30)
MAX_RUNNING_GAP = timedelta(minutes=10)
MAX_PLAUSIBLE_SPEED_KMH = 200
SPIKE_KMH = 50
# clean_route's dirty-point test; the same numbers as core/format.ts.
ROUTE_JITTER_M = 40.0
ROUTE_SPEED_SLACK = 1.5
ROUTE_SPIKE_MAX_GAP = timedelta(minutes=5)

_EARTH_RADIUS_M = 6_371_000


def fix_time(fixed_at: datetime | None, received_at: datetime) -> datetime:
    """When a fix happened: the tracker's own clock, unless it is plainly
    wrong (ahead of arrival, or a month behind), then when it arrived.

    Arrival time alone is wrong whenever a tracker buffers. Out of coverage
    a GT06 unit keeps fixing and uploads the backlog in one burst on
    reconnect, so by arrival a 40-minute drive and a 12-minute halt all
    "happen" within the same second: distance survives, but running time
    and halts collapse to zero."""
    if (
        fixed_at is None
        or fixed_at > received_at + CLOCK_AHEAD_LIMIT
        or fixed_at < received_at - CLOCK_BEHIND_LIMIT
    ):
        return received_at
    return fixed_at


@dataclass(frozen=True)
class ReportFix:
    latitude: float
    longitude: float
    speed_kmh: int | None
    at: datetime
    """The fix's time -- `fix_time(fixed_at, received_at)`."""
    gps_fixed: bool | None = None


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


def plausible_top_speed(fixes: list[ReportFix]) -> int | None:
    """The fastest reading that survives the three rules in the module
    docstring; None if none does. `fixes` in (at, id) order."""
    best: int | None = None
    for i, fix in enumerate(fixes):
        speed = fix.speed_kmh
        if speed is None or fix.gps_fixed is False or speed > MAX_PLAUSIBLE_SPEED_KMH:
            continue
        # A missing neighbour speed reads as 0, like everywhere else here; a
        # fix at either end is judged by the one neighbour it has.
        neighbours = [
            fixes[j].speed_kmh or 0 for j in (i - 1, i + 1) if 0 <= j < len(fixes)
        ]
        if neighbours and all(speed > n + SPIKE_KMH for n in neighbours):
            continue
        if best is None or speed > best:
            best = speed
    return best


def _usable(fix: ReportFix) -> bool:
    return fix.gps_fixed is not False and not (fix.latitude == 0 and fix.longitude == 0)


def _reach(a: ReportFix, b: ReportFix, seconds: float) -> float:
    """The farthest a vehicle could plausibly have gone between two fixes."""
    metres_per_second = ((a.speed_kmh or 0) + (b.speed_kmh or 0)) / 2 / 3.6
    return metres_per_second * max(seconds, 0.0) * ROUTE_SPEED_SLACK + ROUTE_JITTER_M


def _is_dirty(before: ReportFix, fix: ReportFix, after: ReportFix) -> bool:
    seconds_out = (fix.at - before.at).total_seconds()
    seconds_back = (after.at - fix.at).total_seconds()
    limit = ROUTE_SPIKE_MAX_GAP.total_seconds()
    if seconds_out > limit or seconds_back > limit:
        return False

    def dist(a: ReportFix, b: ReportFix) -> float:
        return haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)

    return (
        dist(before, fix) > _reach(before, fix, seconds_out)
        and dist(fix, after) > _reach(fix, after, seconds_back)
        and dist(before, after) <= _reach(before, after, seconds_out + seconds_back)
    )


def clean_route(fixes: list[ReportFix]) -> list[ReportFix]:
    """The fixes a route is measured through -- see the module docstring.
    `fixes` in (at, id) order; so is the result."""
    usable = [f for f in fixes if _usable(f)]
    return [
        fix
        for i, fix in enumerate(usable)
        if not (0 < i < len(usable) - 1 and _is_dirty(usable[i - 1], fix, usable[i + 1]))
    ]


def summarize_device(fixes: list[ReportFix]) -> DeviceTrips:
    """`fixes` must already be in (at, id) order."""
    counted = fixes
    fixes = clean_route(fixes)
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
            gap = fix.at - previous.at
            if (previous.speed_kmh or 0) > 0 and gap <= MAX_RUNNING_GAP:
                running += gap
        if (fix.speed_kmh or 0) == 0:
            if stop_len == 0:
                stop_start = fix.at
            stop_end = fix.at
            stop_len += 1
        else:
            close_stop()
            stop_len = 0
        previous = fix
    close_stop()

    return DeviceTrips(
        distance_km=metres / 1000,
        max_speed_kmh=plausible_top_speed(fixes),
        running_minutes=running.total_seconds() / 60,
        halt_count=len(halts),
        halt_minutes=sum(h.total_seconds() for h in halts) / 60,
        longest_halt_minutes=max((h.total_seconds() for h in halts), default=0) / 60,
        fix_count=len(counted),
        first_fix_at=counted[0].at if counted else None,
        last_fix_at=counted[-1].at if counted else None,
    )
