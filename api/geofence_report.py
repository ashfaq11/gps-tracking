"""
The geofence report: per geofence, over a window, how often vehicles came
and went, which vehicles, and how long they spent inside.

Worked out here from the window's crossings (geofence_events), the same way
for both backends -- crossings are few next to position fixes, so reading
the window's rows and summing them in Python costs little and keeps one
copy of the rules.

Time inside, per (geofence, vehicle), walking its crossings oldest first:
- an enter opens a visit; the next exit closes it and adds its length
- an exit with no enter before it in the window means the vehicle was
  already inside when the window began: it counts from `since`
- a visit still open at the end counts up to `until` (or now, if sooner)
- a repeated enter or exit (a missed crossing in between) adds nothing
"""

from dataclasses import dataclass, field
from datetime import datetime

from .schemas import GeofenceEventOut, GeofenceOut


@dataclass
class VehicleStats:
    device_id: str
    entries: int = 0
    exits: int = 0
    seconds_inside: float = 0.0
    last_event_at: datetime | None = None


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


def summarize_geofences(
    fences: list[GeofenceOut],
    events: list[GeofenceEventOut],
    since: datetime,
    until: datetime,
    now: datetime,
) -> list[FenceStats]:
    """Every geofence in `fences` (quiet ones with zeros), most crossings first.
    `events` may come in any order; only those in [since, until] count."""
    stats = {f.id: FenceStats(geofence_id=f.id, name=f.name) for f in fences}
    by_pair: dict[tuple[int, str], list[GeofenceEventOut]] = {}
    for event in events:
        if event.geofence_id in stats and since <= event.occurred_at <= until:
            by_pair.setdefault((event.geofence_id, event.device_id), []).append(event)

    end = min(until, now)
    for (fence_id, device_id), crossings in by_pair.items():
        crossings.sort(key=lambda e: (e.occurred_at, e.id))
        fence = stats[fence_id]
        vehicle = fence.vehicles.setdefault(device_id, VehicleStats(device_id))
        inside_since: datetime | None = None
        for index, event in enumerate(crossings):
            if event.kind == "enter":
                vehicle.entries += 1
                if inside_since is None:
                    inside_since = event.occurred_at
            else:
                vehicle.exits += 1
                if inside_since is not None:
                    vehicle.seconds_inside += (event.occurred_at - inside_since).total_seconds()
                    inside_since = None
                elif index == 0:
                    vehicle.seconds_inside += (event.occurred_at - since).total_seconds()
            if event.alerted:
                fence.alerts += 1
            vehicle.last_event_at = event.occurred_at
        if inside_since is not None and end > inside_since:
            vehicle.seconds_inside += (end - inside_since).total_seconds()
        fence.entries += vehicle.entries
        fence.exits += vehicle.exits
        if fence.last_event_at is None or vehicle.last_event_at > fence.last_event_at:
            fence.last_event_at = vehicle.last_event_at

    return sorted(
        stats.values(), key=lambda f: (-(f.entries + f.exits), f.name.lower(), f.geofence_id)
    )
