"""
Driving behaviour: a score per vehicle, and the stretches worth a second look.

Plain rules over a vehicle's own fixes -- no model, nothing learned. Unlike
the trip report (api/reports.py), there is no SQL twin: both backends hand
the window's fixes to `score_device`, so the rules exist once and cannot
drift. That is affordable because a driving report covers days, not a
month (MAX_DRIVING_WINDOW), for a fleet of under a hundred vehicles.

Measured over `reports.clean_route` -- the same filtering as every other
figure -- with each gap between consecutive fixes classified by the fix
that opens it, as the trip report does:

- driving: speed > 0 and the next fix within MAX_RUNNING_GAP.
- overspeed: driving at a plausible speed above the limit. Readings without
  a GPS lock or over MAX_PLAUSIBLE_SPEED_KMH are junk, not speeding.
- night: driving that starts between NIGHT_START and NIGHT_END, IST.
- sudden stop / start: speed falling / rising SUDDEN_KMH or more between
  two fixes at most SUDDEN_MAX_GAP apart. A tracker reports every ten
  seconds or so, so this is a firm stop seen coarsely -- not the
  accelerometer-grade "harsh braking" a telematics box measures. It will
  miss a hard brake that lands between two fixes.
- idle: ignition on and not driving. Ignition comes from
  device_ignition_log (sql/schema.sql), which only knows what was reported
  since it was created; a vehicle with no log reads as "unknown" (None),
  never as zero.

The score starts at 100 and loses points for each, weighed against how much
the vehicle drove so a long day is not marked down for being long:

- overspeed: its share of driving time, x OVERSPEED_WEIGHT, at most OVERSPEED_CAP
- sudden stops and starts: per 100 km, x SUDDEN_WEIGHT, at most SUDDEN_CAP
- night: its share of driving time, x NIGHT_WEIGHT, at most NIGHT_CAP
- idle: its share of engine-on time, x IDLE_WEIGHT, at most IDLE_CAP

Under MIN_SCORED_DRIVING of driving there is nothing to judge: score None.

Events are the stretches behind those numbers that someone would want to
be told about: sustained overspeed, a drive at night, a long idle, and a
vehicle parked for LONG_HALT or more.
"""

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .reports import (
    MAX_PLAUSIBLE_SPEED_KMH,
    MAX_RUNNING_GAP,
    PARKED_RADIUS_M,
    ReportFix,
    clean_route,
    haversine_m,
    plausible_top_speed,
)

# The longest window a driving report covers: a week, plus a day so "7
# days" still fits when the request arrives a little late.
MAX_DRIVING_WINDOW = timedelta(days=8)

DEFAULT_SPEED_LIMIT_KMH = 80

# The fleet runs in India; "night" is the driver's night, not UTC's.
IST = timezone(timedelta(hours=5, minutes=30))
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 5

SUDDEN_KMH = 40
SUDDEN_MAX_GAP = timedelta(seconds=15)

MIN_SCORED_DRIVING = timedelta(minutes=5)
OVERSPEED_WEIGHT, OVERSPEED_CAP = 200, 40
SUDDEN_WEIGHT, SUDDEN_CAP = 5, 30
NIGHT_WEIGHT, NIGHT_CAP = 30, 15
IDLE_WEIGHT, IDLE_CAP = 30, 15
# Sudden stops are counted per 100 km; below this a single one would be
# scaled into dozens.
MIN_SCORED_DISTANCE_KM = 10.0

# What makes a stretch an event rather than just part of a total.
OVERSPEED_EVENT = timedelta(minutes=1)
NIGHT_EVENT = timedelta(minutes=2)
LONG_IDLE = timedelta(minutes=15)
LONG_HALT = timedelta(hours=24)
# A drive is one event until the vehicle has been still this long: traffic
# and signals do not split a night drive into a dozen alerts.
DRIVE_BREAK = timedelta(minutes=10)


@dataclass(frozen=True)
class DrivingEvent:
    kind: str
    """'overspeed' | 'night_drive' | 'long_idle' | 'long_halt'"""
    at: datetime
    minutes: float
    max_speed_kmh: int | None = None


@dataclass(frozen=True)
class DeviceDriving:
    distance_km: float
    driving_minutes: float
    max_speed_kmh: int | None
    overspeed_minutes: float
    night_minutes: float
    sudden_stops: int
    sudden_starts: int
    idle_minutes: float | None
    """None when the vehicle has no ignition history to judge from."""
    score: int | None
    """0-100; None when it barely drove."""
    events: list[DrivingEvent] = field(default_factory=list)


def is_night(at: datetime) -> bool:
    hour = at.astimezone(IST).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def ignition_on_spans(
    flips: list[tuple[datetime, bool]], since: datetime, until: datetime
) -> list[tuple[datetime, datetime]] | None:
    """The stretches of [since, until] with ignition on.

    `flips` are (when, state) in time order -- the last one before `since`,
    if there is one, and every one inside the window. None when there are
    none at all: unknown is not off."""
    if not flips:
        return None
    spans: list[tuple[datetime, datetime]] = []
    on_since: datetime | None = None
    for at, state in flips:
        if at > until:
            break
        if state and on_since is None:
            on_since = max(at, since)
        elif not state and on_since is not None:
            if at > on_since:
                spans.append((on_since, at))
            on_since = None
    if on_since is not None and until > on_since:
        spans.append((on_since, until))
    return spans


def _plausible(fix: ReportFix) -> bool:
    return (
        fix.speed_kmh is not None
        and fix.gps_fixed is not False
        and fix.speed_kmh <= MAX_PLAUSIBLE_SPEED_KMH
    )


def score_device(
    fixes: list[ReportFix],
    since: datetime,
    until: datetime,
    *,
    speed_limit_kmh: int = DEFAULT_SPEED_LIMIT_KMH,
    ignition_on: list[tuple[datetime, datetime]] | None = None,
) -> DeviceDriving:
    """`fixes` in (at, id) order, inside [since, until]; `until` already
    capped at now. `ignition_on` from `ignition_on_spans`."""
    route = [f for f in clean_route(fixes) if since <= f.at <= until]

    metres = 0.0
    driving = overspeed = night = 0.0
    sudden_stops = sudden_starts = 0
    events: list[DrivingEvent] = []
    # Driving gaps as (start, end), for the idle arithmetic below.
    drives: list[tuple[datetime, datetime]] = []

    # A stretch of consecutive overspeed gaps.
    over_start: datetime | None = None
    over_seconds = 0.0
    over_max = 0

    def close_overspeed() -> None:
        nonlocal over_start, over_seconds, over_max
        if over_start is not None and over_seconds >= OVERSPEED_EVENT.total_seconds():
            events.append(DrivingEvent("overspeed", over_start, over_seconds / 60, over_max))
        over_start, over_seconds, over_max = None, 0.0, 0

    # One drive: moving gaps, until the vehicle has been still DRIVE_BREAK.
    night_start: datetime | None = None
    night_seconds = 0.0
    still_seconds = 0.0

    def close_drive() -> None:
        nonlocal night_start, night_seconds
        if night_start is not None and night_seconds >= NIGHT_EVENT.total_seconds():
            events.append(DrivingEvent("night_drive", night_start, night_seconds / 60))
        night_start, night_seconds = None, 0.0

    # A stop: stationary gaps in one place, as the trip report counts them.
    stop_start: datetime | None = None
    stop_seconds = 0.0

    def close_stop() -> None:
        nonlocal stop_start, stop_seconds
        if stop_start is not None and stop_seconds >= LONG_HALT.total_seconds():
            events.append(DrivingEvent("long_halt", stop_start, stop_seconds / 60))
        stop_start, stop_seconds = None, 0.0

    for i, fix in enumerate(route):
        nxt = route[i + 1] if i + 1 < len(route) else None
        end = nxt.at if nxt is not None else max(fix.at, until)
        seconds = (end - fix.at).total_seconds()
        speed = fix.speed_kmh or 0
        within_gap = end - fix.at <= MAX_RUNNING_GAP

        if nxt is not None:
            metres += haversine_m(fix.latitude, fix.longitude, nxt.latitude, nxt.longitude)
            if (
                nxt.at - fix.at <= SUDDEN_MAX_GAP
                and _plausible(fix)
                and _plausible(nxt)
            ):
                change = (nxt.speed_kmh or 0) - speed
                if change <= -SUDDEN_KMH:
                    sudden_stops += 1
                elif change >= SUDDEN_KMH:
                    sudden_starts += 1

        parked = speed == 0 and (
            nxt is None
            or haversine_m(fix.latitude, fix.longitude, nxt.latitude, nxt.longitude)
            <= PARKED_RADIUS_M
        )
        if parked:
            if stop_start is None:
                stop_start = fix.at
            stop_seconds += seconds
        else:
            close_stop()

        if speed > 0 and within_gap and nxt is not None:
            driving += seconds
            drives.append((fix.at, end))
            still_seconds = 0.0
            if is_night(fix.at):
                night += seconds
                night_seconds += seconds
                if night_start is None:
                    night_start = fix.at
            if _plausible(fix) and speed > speed_limit_kmh:
                overspeed += seconds
                over_seconds += seconds
                over_max = max(over_max, speed)
                if over_start is None:
                    over_start = fix.at
            else:
                close_overspeed()
        else:
            close_overspeed()
            still_seconds += seconds
            if still_seconds >= DRIVE_BREAK.total_seconds():
                close_drive()
    close_overspeed()
    close_drive()
    close_stop()

    idle: float | None = None
    if ignition_on is not None:
        idle = 0.0
        starts = [start for start, _ in drives]
        for on_start, on_end in ignition_on:
            driven = 0.0
            # The first drive that could overlap: the one before on_start.
            for start, end in drives[max(0, bisect_right(starts, on_start) - 1) :]:
                if start >= on_end:
                    break
                driven += max(0.0, (min(end, on_end) - max(start, on_start)).total_seconds())
            span_idle = max(0.0, (on_end - on_start).total_seconds() - driven)
            idle += span_idle
            if span_idle >= LONG_IDLE.total_seconds():
                events.append(DrivingEvent("long_idle", on_start, span_idle / 60))

    distance_km = metres / 1000
    score: int | None = None
    if driving >= MIN_SCORED_DRIVING.total_seconds():
        penalty = min(OVERSPEED_CAP, overspeed / driving * OVERSPEED_WEIGHT)
        per_100km = (sudden_stops + sudden_starts) / max(distance_km, MIN_SCORED_DISTANCE_KM) * 100
        penalty += min(SUDDEN_CAP, per_100km * SUDDEN_WEIGHT)
        penalty += min(NIGHT_CAP, night / driving * NIGHT_WEIGHT)
        if idle:
            penalty += min(IDLE_CAP, idle / (idle + driving) * IDLE_WEIGHT)
        score = max(0, round(100 - penalty))

    return DeviceDriving(
        distance_km=distance_km,
        driving_minutes=driving / 60,
        max_speed_kmh=plausible_top_speed(route),
        overspeed_minutes=overspeed / 60,
        night_minutes=night / 60,
        sudden_stops=sudden_stops,
        sudden_starts=sudden_starts,
        idle_minutes=None if idle is None else idle / 60,
        score=score,
        events=sorted(events, key=lambda event: event.at, reverse=True),
    )
