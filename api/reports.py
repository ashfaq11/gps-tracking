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
Time: every minute of the window is exactly one of running, halted, short
stop or no data, so the four always add up to the window (ending now, for
a window that has not finished). Each gap between consecutive route fixes
is classified by the fix that opens it:

- moving (speed > 0): running, unless the gap exceeds MAX_RUNNING_GAP --
  then the tracker went quiet (tunnel, flat battery): no data.
- stationary, and the next fix is within PARKED_RADIUS_M of it: stopped --
  however long the gap. A parked tracker often sends nothing but
  heartbeats (no position) until the engine starts, so the whole silent
  night is the stop, not just its first minute.
- stationary, but the next fix is elsewhere: it drove off -- running if
  within MAX_RUNNING_GAP, else no data.
- the last fix: as if repeated at the window's end (still parked, or still
  driving if that is under MAX_RUNNING_GAP away).

Consecutive stopped gaps make one stop. A stop of HALT_THRESHOLD or longer
is a halt -- the threshold the live map's "Halted" badge uses; a shorter
one (traffic, a signal) is a short stop. Whatever is left -- before the
first fix, during long silences -- is no data.

Fixes up to STOP_CONTEXT either side of the window are read too, so a stop
or drive that crosses an edge of it (parked overnight, then "Today") is
seen; only its part inside the window counts. Positions, distance and top
speed use fixes inside the window only.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

HALT_THRESHOLD = timedelta(minutes=10)
# A stationary fix and the next one this close: still the same stop, however
# long the silence between them. Wider than GPS drift on a parked vehicle,
# narrower than any real drive.
PARKED_RADIUS_M = 200.0
# How far either side of the window fixes are read, to see a stop or drive
# that crosses its edges.
STOP_CONTEXT = timedelta(hours=24)
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
    short_stop_minutes: float = 0.0
    no_data_minutes: float = 0.0


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


def summarize_device(
    fixes: list[ReportFix], since: datetime | None = None, until: datetime | None = None
) -> DeviceTrips:
    """`fixes` in (at, id) order, and may run up to STOP_CONTEXT past either
    end of the window [since, until] -- see the module docstring. `until`
    must already be capped at now. Without a window, it is the first to the
    last fix."""
    if since is None:
        since = fixes[0].at if fixes else datetime.min
    if until is None:
        until = fixes[-1].at if fixes else since
    counted = [f for f in fixes if since <= f.at <= until]
    route = clean_route(fixes)
    inside = [f for f in route if since <= f.at <= until]

    metres = sum(
        haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
        for a, b in zip(inside, inside[1:])
    )

    def overlap(start: datetime, end: datetime) -> float:
        return max(0.0, (min(end, until) - max(start, since)).total_seconds())

    halt_s = HALT_THRESHOLD.total_seconds()
    running = 0.0
    halts: list[float] = []
    short_stops: list[float] = []
    stop: float | None = None

    def close_stop() -> None:
        if stop is not None:
            (halts if stop >= halt_s else short_stops).append(stop)

    for i, fix in enumerate(route):
        nxt = route[i + 1] if i + 1 < len(route) else None
        end = nxt.at if nxt is not None else max(fix.at, until)
        within_gap = end - fix.at <= MAX_RUNNING_GAP
        if (fix.speed_kmh or 0) > 0:
            kind = "run" if within_gap else "none"
        elif nxt is None or haversine_m(
            fix.latitude, fix.longitude, nxt.latitude, nxt.longitude
        ) <= PARKED_RADIUS_M:
            kind = "stop"
        else:
            kind = "run" if within_gap else "none"
        seconds = overlap(fix.at, end)
        if kind == "stop":
            stop = (stop or 0.0) + seconds
            continue
        close_stop()
        stop = None
        if kind == "run":
            running += seconds
    close_stop()

    window = max(0.0, (until - since).total_seconds()) if fixes else 0.0
    no_data = max(0.0, window - running - sum(halts) - sum(short_stops))
    return DeviceTrips(
        distance_km=metres / 1000,
        max_speed_kmh=plausible_top_speed(inside),
        running_minutes=running / 60,
        halt_count=len(halts),
        halt_minutes=sum(halts) / 60,
        longest_halt_minutes=max(halts, default=0) / 60,
        fix_count=len(counted),
        first_fix_at=counted[0].at if counted else None,
        last_fix_at=counted[-1].at if counted else None,
        short_stop_minutes=sum(short_stops) / 60,
        no_data_minutes=no_data / 60,
    )
