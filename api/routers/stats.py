"""Aggregates for the dashboard.

Computed in the database rather than by fetching rows and summing them in the
client: a dashboard that pulled every fix to count them would get slower with
every device added.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import current_user, get_repository
from ..repository import LocationRepository
from ..reports import HALT_THRESHOLD
from ..schemas import FixBucket, ReportTotals, StatsSummary, TripReport
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


# A 31-day month is the longest report the dashboard offers; the cap keeps a
# stray request from scanning a device's whole history in one query.
MAX_REPORT_WINDOW = timedelta(days=31)


@router.get(
    "/report",
    response_model=TripReport,
    summary="Trip report -- distance, speed, running time and halts -- per vehicle",
)
async def trip_report(
    since: datetime | None = Query(
        default=None, description="Window start. Defaults to 24 hours before `until`."
    ),
    until: datetime | None = Query(default=None, description="Window end. Defaults to now."),
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
) -> TripReport:
    """
    Per-vehicle distance, top speed, running time and halts (stops of 10
    minutes or more) over the window, plus fleet totals. Definitions are in
    api/reports.py. Scoped like `/summary`: an admin gets every vehicle,
    anyone else only their own; lapsed subscriptions are left out.
    """
    until = _aware(until) if until else datetime.now(timezone.utc)
    since = _aware(since) if since else until - timedelta(hours=24)
    if since >= until:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "`since` must be before `until`.")
    if until - since > MAX_REPORT_WINDOW:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A report covers at most 31 days.")

    devices = await repo.trip_report(since, until, device_ids=_scope(user))
    speeds = [d.max_speed_kmh for d in devices if d.max_speed_kmh is not None]
    return TripReport(
        since=since,
        until=until,
        halt_threshold_minutes=int(HALT_THRESHOLD.total_seconds() // 60),
        totals=ReportTotals(
            devices=len(devices),
            distance_km=sum(d.distance_km for d in devices),
            max_speed_kmh=max(speeds) if speeds else None,
            running_minutes=sum(d.running_minutes for d in devices),
            halt_count=sum(d.halt_count for d in devices),
            halt_minutes=sum(d.halt_minutes for d in devices),
        ),
        devices=devices,
    )


def _aware(value: datetime) -> datetime:
    """A timestamp without an offset is read as UTC, like the rest of the API."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
