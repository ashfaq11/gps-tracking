"""
Alerts history (any signed-in account): every notification the vehicles
raised -- started/stopped, ignition, geofence crossings, overspeed, harsh
driving -- kept for 3 months, the same for the web dashboard and the app.

    GET /alerts                 the history, newest first, a page at a time
                                (?since, ?until, ?kind, ?device_id, ?page, ?size)
    GET /alerts/unread          how many are newer than the account's read mark
    PUT /alerts/seen            move the read mark forward
    GET /alerts/speed-limits    the overspeed limits that were set per vehicle
    PUT /alerts/speed-limits/{device_id}

There is deliberately no DELETE: the history cannot be cleared. Rows are
written by database triggers (sql/schema.sql), so a phone that was off, or a
browser that was closed, loses nothing. An admin sees every vehicle; anyone
else their own, and a geofence's alerts only if they drew it -- and a vehicle
that is not theirs answers 404, as on the device endpoints.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..alerts_repository import AlertRepository, AlertViewer
from ..deps import current_user, get_alerts
from ..schemas import (
    AlertKind,
    AlertPageOut,
    AlertSeenIn,
    AlertUnreadOut,
    SpeedLimitIn,
    SpeedLimitOut,
)
from ..timeutil import as_utc
from ..users_repository import AuthenticatedUser

router = APIRouter(prefix="/alerts", tags=["alerts"])

_KINDS = set(AlertKind.__args__)  # type: ignore[attr-defined]


def _viewer(user: AuthenticatedUser) -> AlertViewer:
    return AlertViewer(
        user_id=user.id,
        is_admin=user.is_admin,
        devices=frozenset(user.devices),
        can_use_geofences=user.can_use_geofences,
    )


def _check_device(user: AuthenticatedUser, device_id: str) -> None:
    if not user.is_admin and device_id not in user.devices:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No device {device_id}")


@router.get("", response_model=AlertPageOut, summary="The alerts history, newest first")
async def list_alerts(
    since: datetime | None = Query(default=None, description="Only alerts at or after this time."),
    until: datetime | None = Query(default=None, description="Only alerts at or before this time."),
    kind: list[AlertKind] | None = Query(default=None, description="Only these kinds (repeat it)."),
    device_id: str | None = Query(default=None, description="Only this vehicle."),
    after_id: int | None = Query(
        default=None, description="Only alerts newer than this id -- how a client polls for new ones."
    ),
    page: int = Query(default=1, ge=1, description="1 is the newest."),
    size: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(current_user),
    repo: AlertRepository = Depends(get_alerts),
) -> AlertPageOut:
    if device_id is not None:
        _check_device(user, device_id)
    entries, total = await repo.list_alerts(
        _viewer(user),
        device_id=device_id,
        kinds=tuple(kind) if kind else None,
        since=as_utc(since) if since else None,
        until=as_utc(until) if until else None,
        after_id=after_id,
        offset=(page - 1) * size,
        limit=size,
    )
    return AlertPageOut(
        page=page, size=size, total=total, pages=max(1, -(-total // size)), entries=entries
    )


@router.get("/unread", response_model=AlertUnreadOut, summary="How many alerts are unread")
async def unread(
    user: AuthenticatedUser = Depends(current_user),
    repo: AlertRepository = Depends(get_alerts),
) -> AlertUnreadOut:
    count, seen = await repo.unread(_viewer(user))
    return AlertUnreadOut(unread=count, seen_id=seen)


@router.put("/seen", response_model=AlertUnreadOut, summary="Mark alerts up to an id as read")
async def mark_seen(
    payload: AlertSeenIn,
    user: AuthenticatedUser = Depends(current_user),
    repo: AlertRepository = Depends(get_alerts),
) -> AlertUnreadOut:
    """Only moves forward. Reading on one screen clears the badge on every other."""
    viewer = _viewer(user)
    await repo.mark_seen(viewer, payload.seen_id)
    count, seen = await repo.unread(viewer)
    return AlertUnreadOut(unread=count, seen_id=seen)


@router.get(
    "/speed-limits",
    response_model=list[SpeedLimitOut],
    summary="The overspeed limits that were set",
    response_description="A vehicle not listed has the default, 80 km/h.",
)
async def speed_limits(
    user: AuthenticatedUser = Depends(current_user),
    repo: AlertRepository = Depends(get_alerts),
) -> list[SpeedLimitOut]:
    scope = None if user.is_admin else frozenset(user.devices)
    limits = await repo.speed_limits(scope)
    return [SpeedLimitOut(device_id=d, limit_kmh=v) for d, v in sorted(limits.items())]


@router.put(
    "/speed-limits/{device_id}",
    response_model=SpeedLimitOut,
    summary="Set a vehicle's overspeed limit",
    responses={404: {"description": "Not one of your vehicles."}},
)
async def set_speed_limit(
    device_id: str,
    payload: SpeedLimitIn,
    user: AuthenticatedUser = Depends(current_user),
    repo: AlertRepository = Depends(get_alerts),
) -> SpeedLimitOut:
    """Applies to alerts from now on, for everyone who sees the vehicle."""
    _check_device(user, device_id)
    await repo.set_speed_limit(device_id, payload.limit_kmh)
    return SpeedLimitOut(device_id=device_id, limit_kmh=payload.limit_kmh)
