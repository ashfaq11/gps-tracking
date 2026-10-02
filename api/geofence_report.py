"""
The geofence report: per geofence, over a window, how often vehicles came
and went, which vehicles, and how long they spent inside and outside.

Worked out here from the window's crossings (geofence_events), the same way
for both backends -- crossings are few next to position fixes, so reading
the window's rows and summing them in Python costs little and keeps one
copy of the rules.

Time, per (geofence, vehicle), over the window -- ending now if it has not
finished yet:
- inside at the start if its last crossing before the window was an enter;
  with none before the window, if its first crossing in it is an exit
- an enter opens a visit; the next exit closes it
- a visit still open at the end counts up to the end
- a repeated enter or exit (a missed crossing in between) changes nothing
- outside is the rest of the window, so inside + outside = the window
- a vehicle that was inside all along, with no crossing in the window, is
  listed too (its last crossing before it was an enter)

Totals are per vehicle, not per geofence: a vehicle inside two overlapping
geofences at once is inside for that time once. So for each vehicle,
inside any geofence + outside all of them = the window, and the totals are
those summed over vehicles -- one vehicle over one day adds up to one day.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .schemas import GeofenceEventOut, GeofenceOut

Interval = tuple[datetime, datetime]


@dataclass
class VehicleStats:
    device_id: str
    entries: int = 0
    exits: int = 0
    seconds_inside: float = 0.0
    seconds_outside: float = 0.0
    last_event_at: datetime | None = None
    intervals: list[Interval] = field(default_factory=list)


@dataclass
class FenceStats:
    geofence_id: int
    name: str
    entries: int = 0
    exits: int = 0
    alerts: int = 0
    last_event_at: datetime | None = None
    vehicles: dict[str, VehicleStats] = field(default_factory=dict)

    @property
    def seconds_inside(self) -> float:
        return sum(v.seconds_inside for v in self.vehicles.values())

    @property
    def seconds_outside(self) -> float:
        return sum(v.seconds_outside for v in self.vehicles.values())


@dataclass
class ReportTime:
    """Across geofences, per vehicle -- see the module docstring."""

    window_seconds: float
    seconds_inside: float
    seconds_outside: float


def _union_seconds(intervals: list[Interval]) -> float:
    total = 0.0
    current: Interval | None = None
    for start, end in sorted(intervals):
        if current is None or start > current[1]:
            if current is not None:
                total += (current[1] - current[0]).total_seconds()
            current = (start, end)
        elif end > current[1]:
            current = (current[0], end)
    if current is not None:
        total += (current[1] - current[0]).total_seconds()
    return total


def summarize_geofences(
    fences: list[GeofenceOut],
    events: list[GeofenceEventOut],
    since: datetime,
    until: datetime,
    now: datetime,
    prior: list[GeofenceEventOut] | None = None,
) -> tuple[list[FenceStats], ReportTime]:
    """Every geofence in `fences` (quiet ones with zeros), most crossings
    first, and the window's time across them. `events` may come in any
    order; only those in [since, until] count. `prior` is each pair's last
    crossing before `since`, if any."""
    stats = {f.id: FenceStats(geofence_id=f.id, name=f.name) for f in fences}
    end = max(since, min(until, now))
    window = (end - since).total_seconds()

    by_pair: dict[tuple[int, str], list[GeofenceEventOut]] = {}
    for event in events:
        if event.geofence_id in stats and since <= event.occurred_at <= until:
            by_pair.setdefault((event.geofence_id, event.device_id), []).append(event)
    inside_at_start: dict[tuple[int, str], bool] = {}
    for event in prior or []:
        if event.geofence_id in stats and event.occurred_at < since:
            key = (event.geofence_id, event.device_id)
            inside_at_start[key] = event.kind == "enter"
            if event.kind == "enter":
                by_pair.setdefault(key, [])

    for (fence_id, device_id), crossings in by_pair.items():
        crossings.sort(key=lambda e: (e.occurred_at, e.id))
        fence = stats[fence_id]
        vehicle = fence.vehicles.setdefault(device_id, VehicleStats(device_id))
        known = inside_at_start.get((fence_id, device_id))
        if known is None:
            known = bool(crossings) and crossings[0].kind == "exit"
        inside_since: datetime | None = since if known else None
        for event in crossings:
            at = min(event.occurred_at, end)
            if event.kind == "enter":
                vehicle.entries += 1
                if inside_since is None:
                    inside_since = at
            else:
                vehicle.exits += 1
                if inside_since is not None:
                    vehicle.intervals.append((inside_since, at))
                    inside_since = None
            if event.alerted:
                fence.alerts += 1
            vehicle.last_event_at = event.occurred_at
        if inside_since is not None and end > inside_since:
            vehicle.intervals.append((inside_since, end))
        vehicle.seconds_inside = sum((b - a).total_seconds() for a, b in vehicle.intervals)
        vehicle.seconds_outside = max(0.0, window - vehicle.seconds_inside)
        fence.entries += vehicle.entries
        fence.exits += vehicle.exits
        if vehicle.last_event_at is not None and (
            fence.last_event_at is None or vehicle.last_event_at > fence.last_event_at
        ):
            fence.last_event_at = vehicle.last_event_at

    per_vehicle: dict[str, list[Interval]] = {}
    for fence in stats.values():
        for vehicle in fence.vehicles.values():
            per_vehicle.setdefault(vehicle.device_id, []).extend(vehicle.intervals)
    inside = sum(_union_seconds(intervals) for intervals in per_vehicle.values())
    time = ReportTime(
        window_seconds=window,
        seconds_inside=inside,
        seconds_outside=max(0.0, window * len(per_vehicle) - inside),
    )

    ordered = sorted(
        stats.values(), key=lambda f: (-(f.entries + f.exits), f.name.lower(), f.geofence_id)
    )
    return ordered, time
