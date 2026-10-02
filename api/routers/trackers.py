"""
Tracker allowlist (admin): which IMEIs the gateway accepts at login, and the
unknown ones it refused. See sql/schema.sql's device_allowlist for the rules
-- including why this narrows, but cannot close, IMEI impersonation.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import get_trackers, require_admin
from ..schemas import (
    AllowedTrackerOut,
    AllowTrackerIn,
    LoginAttemptOut,
    TrackerSettingsIn,
    TrackerSettingsOut,
)
from ..trackers_repository import TrackerRepository
from ..users_repository import AuthenticatedUser

router = APIRouter(prefix="/trackers", tags=["trackers"], dependencies=[Depends(require_admin)])


@router.get(
    "/allowlist",
    response_model=list[AllowedTrackerOut],
    summary="Trackers the gateway accepts",
)
async def list_allowed(trackers: TrackerRepository = Depends(get_trackers)):
    return await trackers.list_allowed()


@router.post(
    "/allowlist",
    response_model=AllowedTrackerOut,
    status_code=status.HTTP_201_CREATED,
    summary="Allow a tracker (or approve one waiting)",
)
async def allow(
    payload: AllowTrackerIn,
    admin: AuthenticatedUser = Depends(require_admin),
    trackers: TrackerRepository = Depends(get_trackers),
) -> AllowedTrackerOut:
    """Takes effect on the tracker's next login attempt -- GT06 units retry
    every few seconds to minutes, so usually almost at once. Allowing one
    already allowed is harmless and returns it unchanged."""
    return await trackers.allow(payload.device_id, admin.id, requester=admin.username)


@router.delete(
    "/allowlist/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop accepting a tracker",
    responses={404: {"description": "It was not on the list."}},
)
async def disallow(device_id: str, trackers: TrackerRepository = Depends(get_trackers)):
    """Refused from its next login on. A connection already open stays open
    until the tracker reconnects (or the gateway restarts); its history is
    kept. A tracker someone still owns is re-allowed only if it is assigned
    again."""
    if not await trackers.disallow(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{device_id} is not on the list")


@router.get(
    "/attempts",
    response_model=list[LoginAttemptOut],
    summary="Unknown trackers the gateway refused",
)
async def list_attempts(trackers: TrackerRepository = Depends(get_trackers)):
    """A newly installed tracker nobody owns yet shows up here first --
    approve it with POST /trackers/allowlist."""
    return await trackers.list_attempts()


@router.delete(
    "/attempts/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Dismiss a refused tracker from the list",
    responses={404: {"description": "No refused attempt for it."}},
)
async def dismiss_attempt(device_id: str, trackers: TrackerRepository = Depends(get_trackers)):
    """Still refused; it only reappears if it tries again."""
    if not await trackers.dismiss_attempt(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No attempt from {device_id}")


@router.get(
    "/settings",
    response_model=TrackerSettingsOut,
    summary="Auto-approve new trackers, or hold them for approval",
)
async def get_settings(trackers: TrackerRepository = Depends(get_trackers)):
    return await trackers.settings()


@router.put(
    "/settings",
    response_model=TrackerSettingsOut,
    summary="Switch auto-approve for new trackers on or off",
)
async def set_settings(
    payload: TrackerSettingsIn,
    admin: AuthenticatedUser = Depends(require_admin),
    trackers: TrackerRepository = Depends(get_trackers),
) -> TrackerSettingsOut:
    """
    **On:** any tracker that logs in is admitted at once, no approval needed,
    and added to the allowlist as `auto` -- including the ones waiting in
    `/trackers/attempts`, on their next login attempt.

    **Off (hold, the default):** a tracker nobody approved is refused and
    waits in `/trackers/attempts`. Trackers auto-approved earlier stay
    allowed; remove one from the allowlist to refuse it again.

    Applies when the gateway runs with GATEWAY_ALLOWLIST=enforce (the
    default); with `log` or `off` every tracker is admitted anyway.
    """
    return await trackers.set_auto_approve(payload.auto_approve, admin.id, requester=admin.username)
