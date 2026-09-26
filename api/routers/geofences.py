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

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import current_user, get_geofences
from ..geofences_repository import GeofenceFields, GeofenceRepository, GeofenceShape
from ..schemas import GeofenceEventOut, GeofenceIn, GeofenceOut, GeofenceUpdate, GeoPoint
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
    )


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
