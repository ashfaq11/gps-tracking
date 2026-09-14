"""Aggregates for the dashboard.

Computed in the database rather than by fetching rows and summing them in the
client: a dashboard that pulled every fix to count them would get slower with
every device added.
"""

from fastapi import APIRouter, Depends, Query

from ..deps import current_user, get_repository
from ..repository import LocationRepository
from ..schemas import FixBucket, StatsSummary
from ..users_repository import AuthenticatedUser

router = APIRouter(prefix="/stats", tags=["stats"])


def _scope(user: AuthenticatedUser) -> frozenset[str] | None:
    return None if user.is_admin else frozenset(user.devices)


@router.get(
    "/summary",
    response_model=StatsSummary,
    summary="Headline counts for the devices this account can see",
)
async def summary(
    active_minutes: int = Query(
        default=15,
        ge=1,
        le=1440,
        description="A device counts as active if it reported within this many minutes.",
    ),
    recent_hours: int = Query(default=24, ge=1, le=720),
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
) -> StatsSummary:
    """
    Device and fix counts, plus how many are recent enough to matter.

    An admin gets fleet-wide numbers; anyone else gets numbers computed over
    only the devices assigned to their account -- otherwise a scoped account
    could read the whole fleet's totals straight off this endpoint even with
    every device-level route correctly locked down. Devices with a lapsed
    subscription are excluded from these counts too, for everyone.
    """
    data = await repo.summary(
        active_minutes=active_minutes, recent_hours=recent_hours, device_ids=_scope(user)
    )
    return StatsSummary(
        active_window_minutes=active_minutes,
        recent_window_hours=recent_hours,
        **data,
    )


@router.get(
    "/fixes",
    response_model=list[FixBucket],
    summary="Fixes over time, for the devices this account can see",
    response_description="Buckets in ascending time order. Empty buckets are omitted.",
)
async def fixes_over_time(
    hours: int = Query(default=24, ge=1, le=720, description="How far back to look."),
    bucket_minutes: int = Query(default=60, ge=1, le=1440, description="Bucket width."),
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
) -> list[FixBucket]:
    """
    Ingestion volume over time — the chart that shows at a glance whether
    devices stopped reporting. Scoped the same way `/summary` is.
    """
    return await repo.fixes_over_time(
        hours=hours, bucket_minutes=bucket_minutes, device_ids=_scope(user)
    )
