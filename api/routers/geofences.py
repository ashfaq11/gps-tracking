"""
Geofences: draw an area, pick the vehicles it watches, and get told when one
of them leaves (or enters) it.

Any signed-in account can draw geofences, for the vehicles it can see. An
admin sees and can change every geofence; anyone else only their own -- and,
following the device endpoints' rule, someone else's geofence answers 404,
exactly like one that does not exist.

Detecting a crossing is not done here at all -- see the `check_geofences`
trigger in sql/schema.sql and api/push.py for the alert.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import current_user, get_geofences
from ..geofence_report import summarize_geofences
from ..geofences_repository import GeofenceFields, GeofenceRepository, GeofenceShape
from ..schemas import (
    GeofenceEventOut,
    GeofenceIn,
    GeofenceOut,
    GeofenceReport,
    GeofenceReportRow,
    GeofenceReportTotals,
    GeofenceReportVehicle,
    GeofenceUpdate,
    GeoPoint,
)
from ..users_repository import AuthenticatedUser

router = APIRouter(prefix="/geofences", tags=["geofences"])


def _not_found(geofence_id: int) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No geofence {geofence_id}")


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _owner_filter(user: AuthenticatedUser) -> int | None:
    """None for an admin (every geofence); otherwise only the caller's own."""
    return None if user.is_admin else user.id


def _device_scope(user: AuthenticatedUser) -> frozenset[str] | None:
    return None if user.is_admin else frozenset(user.devices)


def _shape(
    kind: str,
    vertices: list[GeoPoint] | None,
    center: GeoPoint | None,
    radius_m: float | None,
) -> GeofenceShape:
    if kind == "polygon":
        if not vertices or len(vertices) < 3:
            raise _invalid("A polygon geofence needs at least 3 points")
        return GeofenceShape.polygon(vertices)
    if center is None or radius_m is None:
        raise _invalid("A circle geofence needs a center and a radius_m")
    return GeofenceShape.circle(center, radius_m)


def _checked_devices(user: AuthenticatedUser, device_ids: list[str]) -> list[str]:
    """De-duplicated, and every one visible to the caller -- the same 'not
    yours reads as does not exist' answer the device endpoints give."""
    cleaned = sorted({d.strip() for d in device_ids if d and d.strip()})
    scope = _device_scope(user)
    if scope is not None:
        for device_id in cleaned:
            if device_id not in scope:
                raise _invalid(f"No device {device_id}")
    return cleaned


async def _visible(
    geofence_id: int, user: AuthenticatedUser, repo: GeofenceRepository
) -> GeofenceOut:
    fence = await repo.get_geofence(geofence_id)
    if fence is None or (not user.is_admin and fence.owner_id != user.id):
        raise _not_found(geofence_id)
    return fence


@router.get(
    "/events",
    response_model=list[GeofenceEventOut],
    summary="The geofence report: every time a vehicle crossed a geofence's edge",
    response_description="Newest first.",
)
async def list_events(
    geofence_id: int | None = Query(default=None, description="Only this geofence."),
    device_id: str | None = Query(default=None, description="Only this vehicle."),
    after_id: int | None = Query(
        default=None,
        description="Only crossings newer than this event id -- how a client polls for new "
        "alerts without re-reading ones it has seen.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    since: datetime | None = Query(
        default=None, description="Only crossings at or after this time -- e.g. a day's start."
    ),
    until: datetime | None = Query(default=None, description="Only crossings at or before this time."),
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> list[GeofenceEventOut]:
    """
    Both directions are listed -- leaving and entering -- whether or not
    that geofence was set to alert for it (`alerted` says which did). An
    admin sees every geofence's crossings; anyone else only their own
    geofences', for their own vehicles.
    """
    return await repo.list_events(
        owner_id=_owner_filter(user),
        device_scope=_device_scope(user),
        geofence_id=geofence_id,
        device_id=device_id,
        after_id=after_id,
        limit=limit,
        since=_aware(since) if since else None,
        until=_aware(until) if until else None,
    )


# Same cap as the trip report; and a window's crossings are read in one go,
# which a busy fleet over a month could make large -- past this many the
# report says it is partial rather than reading without bound.
MAX_REPORT_WINDOW = timedelta(days=31)
MAX_REPORT_EVENTS = 50_000


@router.get(
    "/report",
    response_model=GeofenceReport,
    summary="Geofence report: entries, exits, vehicles and time inside, per geofence",
)
async def geofence_report(
    since: datetime | None = Query(
        default=None, description="Window start. Defaults to 24 hours before `until`."
    ),
    until: datetime | None = Query(default=None, description="Window end. Defaults to now."),
    geofence_id: int | None = Query(default=None, description="Only this geofence."),
    device_id: str | None = Query(default=None, description="Only this vehicle."),
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> GeofenceReport:
    """
    Per geofence over the window: entries, exits, alerts sent, and each
    vehicle's crossings and time spent inside (rules in
    api/geofence_report.py). Scoped like `/events`: an admin sees every
    geofence, anyone else only their own geofences and their own vehicles.
    Quiet geofences are listed too, with zeros.

    `geofence_id` and `device_id` narrow everything -- crossings, times and
    totals -- the same way they narrow `/events`. With a vehicle, the
    geofences listed are the ones that vehicle is assigned to or crossed.
    A geofence or vehicle the caller cannot see reads as an empty report.
    """
    now = datetime.now(timezone.utc)
    until = _aware(until) if until else now
    since = _aware(since) if since else until - timedelta(hours=24)
    if since >= until:
        raise _invalid("`since` must be before `until`.")
    if until - since > MAX_REPORT_WINDOW:
        raise _invalid("A report covers at most 31 days.")

    owner_id = _owner_filter(user)
    fences = await repo.list_geofences(owner_id)
    events = await repo.list_events(
        owner_id=owner_id,
        device_scope=_device_scope(user),
        geofence_id=geofence_id,
        device_id=device_id,
        since=since,
        until=until,
        limit=MAX_REPORT_EVENTS,
    )
    prior = [
        event
        for event in await repo.last_crossings_before(
            owner_id=owner_id, device_scope=_device_scope(user), before=since
        )
        if (geofence_id is None or event.geofence_id == geofence_id)
        and (device_id is None or event.device_id == device_id)
    ]
    if geofence_id is not None:
        fences = [f for f in fences if f.id == geofence_id]
    if device_id is not None:
        scope = _device_scope(user)
        if scope is not None and device_id not in scope:
            fences = []
        else:
            crossed = {e.geofence_id for e in events} | {e.geofence_id for e in prior}
            fences = [f for f in fences if device_id in f.device_ids or f.id in crossed]
    stats, time = summarize_geofences(fences, events, since, until, now, prior)
    return GeofenceReport(
        since=since,
        until=until,
        truncated=len(events) >= MAX_REPORT_EVENTS,
        totals=GeofenceReportTotals(
            geofences=sum(1 for f in stats if f.entries or f.exits),
            entries=sum(f.entries for f in stats),
            exits=sum(f.exits for f in stats),
            alerts=sum(f.alerts for f in stats),
            vehicles=len({v for f in stats for v in f.vehicles}),
            window_minutes=time.window_seconds / 60,
            time_inside_minutes=time.seconds_inside / 60,
            time_outside_minutes=time.seconds_outside / 60,
        ),
        geofences=[
            GeofenceReportRow(
                geofence_id=f.geofence_id,
                name=f.name,
                entries=f.entries,
                exits=f.exits,
                alerts=f.alerts,
                time_inside_minutes=f.seconds_inside / 60,
                time_outside_minutes=f.seconds_outside / 60,
                last_event_at=f.last_event_at,
                vehicles=[
                    GeofenceReportVehicle(
                        device_id=v.device_id,
                        entries=v.entries,
                        exits=v.exits,
                        time_inside_minutes=v.seconds_inside / 60,
                        time_outside_minutes=v.seconds_outside / 60,
                        last_event_at=v.last_event_at,
                    )
                    for v in sorted(
                        f.vehicles.values(), key=lambda v: (-(v.entries + v.exits), v.device_id)
                    )
                ],
            )
            for f in stats
        ],
    )


def _aware(value: datetime) -> datetime:
    """A timestamp without an offset is read as UTC, like the rest of the API."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("", response_model=list[GeofenceOut], summary="List geofences")
async def list_geofences(
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> list[GeofenceOut]:
    """An admin's list is every geofence; anyone else's is their own."""
    return await repo.list_geofences(_owner_filter(user))


@router.post(
    "",
    response_model=GeofenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Draw a geofence",
)
async def create_geofence(
    payload: GeofenceIn,
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> GeofenceOut:
    """
    Nothing is reported for a vehicle until its next position after this
    -- that position only records which side it is on. So drawing a
    geofence round a vehicle that is already outside it does not report an
    exit that never happened; it alerts the next time it actually crosses.
    """
    shape = _shape(payload.kind, payload.vertices, payload.center, payload.radius_m)
    fields = GeofenceFields(
        name=payload.name.strip(),
        device_ids=_checked_devices(user, payload.device_ids),
        alert_on_exit=payload.alert_on_exit,
        alert_on_enter=payload.alert_on_enter,
    )
    if not fields.name:
        raise _invalid("A geofence needs a name")
    return await repo.create_geofence(user.id, shape, fields)


@router.get("/{geofence_id}", response_model=GeofenceOut, summary="One geofence")
async def get_geofence(
    geofence_id: int,
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> GeofenceOut:
    return await _visible(geofence_id, user, repo)


@router.patch("/{geofence_id}", response_model=GeofenceOut, summary="Change a geofence")
async def update_geofence(
    geofence_id: int,
    payload: GeofenceUpdate,
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> GeofenceOut:
    current = await _visible(geofence_id, user, repo)
    sent = payload.model_fields_set

    shape = None
    if sent & {"kind", "vertices", "center", "radius_m"}:
        kind = payload.kind or current.kind
        same_kind = kind == current.kind
        shape = _shape(
            kind,
            payload.vertices if "vertices" in sent else (current.vertices if same_kind else None),
            payload.center if "center" in sent else (current.center if same_kind else None),
            payload.radius_m if "radius_m" in sent else (current.radius_m if same_kind else None),
        )

    name = None
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise _invalid("A geofence needs a name")

    fields = GeofenceFields(
        name=name,
        device_ids=(
            _checked_devices(user, payload.device_ids) if payload.device_ids is not None else None
        ),
        alert_on_exit=payload.alert_on_exit,
        alert_on_enter=payload.alert_on_enter,
    )
    updated = await repo.update_geofence(geofence_id, shape, fields)
    if updated is None:
        raise _not_found(geofence_id)
    return updated


@router.delete(
    "/{geofence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a geofence, and its report",
)
async def delete_geofence(
    geofence_id: int,
    user: AuthenticatedUser = Depends(current_user),
    repo: GeofenceRepository = Depends(get_geofences),
) -> None:
    await _visible(geofence_id, user, repo)
    if not await repo.delete_geofence(geofence_id):
        raise _not_found(geofence_id)
