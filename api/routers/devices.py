from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..config import ApiConfig
from ..deps import current_user, get_config, get_repository, get_users, require_admin
from ..repository import DeviceDetails, LocationRepository
from ..schemas import (
    DeviceClaimRequest,
    DeviceOut,
    DeviceProfileOut,
    DeviceProfileUpdate,
    DeviceSubscriptionHistoryOut,
    DeviceSubscriptionOut,
    DeviceSubscriptionUpdate,
    LocationOut,
    SubscriptionStatus,
)
from ..users_repository import AuthenticatedUser, UserRepository

router = APIRouter(prefix="/devices", tags=["devices"])

# Onboarding a device onto metering without naming a date gets this term,
# starting from today -- a year is the common billing cycle for tracking
# hardware, and 365 fixed days sidesteps leap-year ambiguity in "1 year".
DEFAULT_SUBSCRIPTION_TERM = timedelta(days=365)


def _status(end_date: datetime | None) -> SubscriptionStatus:
    return "active" if (end_date is None or end_date > datetime.now(timezone.utc)) else "expired"


def _subscription_out(device_id: str, details: DeviceDetails) -> DeviceSubscriptionOut:
    return DeviceSubscriptionOut(
        device_id=device_id,
        subscription_end_date=details.subscription_end_date,
        subscription_status=_status(details.subscription_end_date),
        installed_at=details.installed_at,
        sim_expiry_date=details.sim_expiry_date,
    )


def _scope(user: AuthenticatedUser) -> frozenset[str] | None:
    """None means "no filter" -- an admin sees the whole fleet. Anyone else
    is limited to exactly the devices assigned to their account, which may
    be an empty set."""
    return None if user.is_admin else frozenset(user.devices)


@router.get(
    "",
    response_model=list[DeviceOut],
    summary="List every device this account can see",
    response_description="Devices ordered by most recently seen first.",
)
async def list_devices(
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
    users: UserRepository = Depends(get_users),
) -> list[DeviceOut]:
    """
    One row per device that has ever reported a position, been claimed, or
    been given a name -- last-seen time and fix count are null/0 for a
    device that has never actually transmitted; that's how to tell the two
    apart.

    An admin sees the whole fleet; anyone else sees only the devices assigned
    to their account. A device whose subscription has lapsed is still listed
    here, with `subscription_status: "expired"` — this endpoint is how you
    find out it needs renewing — but its `/latest` and `/locations` are
    hidden until it is. `installed_at` and `sim_expiry_date`, when set, ride
    along too — see `GET /devices/{device_id}/subscription` for how to set them.

    `owner_username` is null for a device nobody has claimed yet -- an admin
    onboards it via `POST /users`, `PATCH /users/{id}`, `POST /devices/claim`,
    or by naming/assigning it with no report yet required. Devices are
    assembled by LocationRepository, which has no notion of accounts, so
    ownership is looked up here and merged in -- one extra query, not a
    cross-repository join.
    """
    scope = _scope(user)
    devices = await repo.list_devices(device_ids=scope)
    seen_ids = {d.device_id for d in devices}

    if scope is None:
        # Admin: every claimed device, plus every named-but-unclaimed one --
        # whether or not either has actually reported a position yet.
        owners = await users.device_owners(None)
        extra_ids = (set(owners) | await repo.profiled_device_ids()) - seen_ids
    else:
        # A scoped account's own visibility list already is the full set it
        # may see -- a self-claim or an admin's assignment put every one of
        # these ids there, reported or not, so there is nothing further to
        # union in.
        owners = await users.device_owners(list(scope))
        extra_ids = scope - seen_ids

    stubs = [await repo.device_stub(device_id) for device_id in extra_ids]
    combined = devices + stubs
    return [d.model_copy(update={"owner_username": owners.get(d.device_id)}) for d in combined]


@router.get(
    "/{device_id}/latest",
    response_model=LocationOut,
    summary="Most recent fix for a device",
    responses={
        404: {
            "description": "No fixes stored for this device, it is not assigned to you, "
            "or its subscription has expired."
        }
    },
)
async def latest_location(
    device_id: str,
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
) -> LocationOut:
    """
    Where the device is now — the single newest row, by `received_at`.

    A device with a lapsed subscription answers 404 here too, the same as
    one with no fixes at all — `repo.latest_for_device` hides it regardless
    of who is asking, admins included; see `GET /devices` to tell the two
    apart.
    """
    scope = _scope(user)
    if scope is not None and device_id not in scope:
        # Same 404 as "no fixes for this device" -- a scoped account cannot
        # tell an unassigned device apart from one that does not exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No fixes for device {device_id}"
        )
    location = await repo.latest_for_device(device_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No fixes for device {device_id}"
        )
    return location


@router.get(
    "/{device_id}/locations",
    response_model=list[LocationOut],
    summary="Location history for a device",
    response_description="Fixes newest first. Empty list for an unknown or unassigned device.",
    responses={
        422: {"description": "limit below 1, a malformed timestamp, or until earlier than since."}
    },
)
async def location_history(
    device_id: str,
    limit: int = Query(default=100, ge=1, description="Maximum rows; clamped server-side."),
    since: datetime | None = Query(
        default=None, description="Only fixes received at or after this time, e.g. 2026-09-01T00:00:00Z."
    ),
    until: datetime | None = Query(
        default=None, description="Only fixes received at or before this time. Inclusive."
    ),
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
    config: ApiConfig = Depends(get_config),
) -> list[LocationOut]:
    """
    Recent fixes, newest first — the trail you would draw on a map.

    `since` and `until` are both inclusive and both optional, so this serves an
    open-ended window as well as a closed date range. Send timestamps as UTC
    (`...Z`) or with an explicit offset: a naive timestamp is ambiguous, and the
    dashboard's date pickers are in the operator's local zone.

    `limit` is clamped to the server's maximum (default 1000) rather than
    rejected, so asking for everything returns a bounded page instead of an
    error. An unknown device returns `[]`, not a 404 -- and so does one that
    exists but is not assigned to this account, for the same reason `/latest`
    answers 404 rather than 403: the caller cannot distinguish the two. A
    device with a lapsed subscription also returns `[]`, for everyone,
    admins included, until it is renewed.
    """
    if since is not None and until is not None and until < since:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`until` must not be earlier than `since`.",
        )
    scope = _scope(user)
    if scope is not None and device_id not in scope:
        return []
    return await repo.history_for_device(
        device_id, limit=min(limit, config.max_page_size), since=since, until=until
    )


@router.patch(
    "/{device_id}",
    response_model=DeviceProfileOut,
    summary="Set a device's display name and/or map marker",
    responses={
        404: {"description": "This device is not assigned to you (scoped accounts only)."}
    },
)
async def update_device_profile(
    device_id: str,
    payload: DeviceProfileUpdate,
    user: AuthenticatedUser = Depends(current_user),
    repo: LocationRepository = Depends(get_repository),
) -> DeviceProfileOut:
    """
    Cosmetic only -- not billing state (see `.../subscription` for that) and
    not an allowlist (there isn't one). Any signed-in account may rename or
    re-icon a device it can see; this is personalization, the same trust
    level as choosing a UI language, not an admin action.

    Works for a device id that has never reported a fix, same as the
    subscription endpoints -- the response is not `DeviceOut` for that
    reason, since `last_seen`/`fix_count` would have nothing to report.

    Only the fields you send are changed. Send `"name": null` (or `""`) to
    clear the name back to showing the bare device id.
    """
    scope = _scope(user)
    if scope is not None and device_id not in scope:
        # Same 404 as an unassigned device everywhere else in this router --
        # a scoped account cannot tell "not yours" from "doesn't exist".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Device {device_id} not found"
        )

    fields = payload.model_fields_set
    profile_kwargs = {}
    if "name" in fields:
        # A blank or whitespace-only name clears it, same as the device
        # names UI's own "an empty name clears it" rule.
        trimmed = payload.name.strip() if payload.name else ""
        profile_kwargs["name"] = trimmed[:64] or None
    if "icon" in fields and payload.icon is not None:
        profile_kwargs["icon"] = payload.icon
    if profile_kwargs:
        await repo.set_device_profile(device_id, **profile_kwargs)

    details = await repo.device_details(device_id)
    return DeviceProfileOut(device_id=device_id, name=details.name, icon=details.icon)


@router.post(
    "/claim",
    response_model=DeviceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add another device to the signed-in account",
    response_description="The newly claimed device, as it appears in GET /devices.",
    responses={
        404: {"description": "That device has never reported a position."},
        409: {"description": "The device already has an owner."},
    },
)
async def claim_device(
    payload: DeviceClaimRequest,
    user: AuthenticatedUser = Depends(current_user),
    users: UserRepository = Depends(get_users),
    repo: LocationRepository = Depends(get_repository),
) -> DeviceOut:
    """
    A second vehicle for an account that already exists -- the same rules as
    signup, and the same shared implementation, so the two cannot drift.
    """
    # Imported here rather than at module scope: routers/users.py imports
    # nothing from this module, and keeping it that way avoids a cycle.
    from .users import claim_or_conflict

    await claim_or_conflict(payload.device_id, user.id, users, repo)
    devices = await repo.list_devices(device_ids=frozenset({payload.device_id}))
    return devices[0]


@router.delete(
    "/{device_id}/claim",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    summary="Release a device so someone else can claim it",
    responses={
        403: {"description": "The caller is not an administrator."},
        404: {"description": "That device has no owner to release."},
    },
)
async def release_device(
    device_id: str, users: UserRepository = Depends(get_users)
) -> None:
    """
    Hand a device back to nobody -- the resale path.

    Admin only, and deliberately so: this is the one lever that lets a
    device change hands, and "first claim wins" would mean nothing if the
    current owner (or anyone else) could pull it. The device's stored
    history is untouched; only the ownership and the visibility it granted
    are removed.
    """
    if not await users.release_device(device_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id} is not claimed by anyone",
        )


# --- subscription management (admin) ----------------------------------------
#
# Billing state on the device, independent of dashboard accounts and their
# device scoping above. A lapsed device's location and history are hidden
# from every viewer -- admins included -- by list_devices/latest_for_device/
# history_for_device themselves (see repository.py), so these endpoints exist
# only to read and change that state, not to view the device's data.


@router.get(
    "/{device_id}/subscription",
    response_model=DeviceSubscriptionOut,
    dependencies=[Depends(require_admin)],
    summary="A device's current subscription status",
)
async def get_subscription(
    device_id: str, repo: LocationRepository = Depends(get_repository)
) -> DeviceSubscriptionOut:
    """No row (the common case) means unmetered: this device never expires."""
    details = await repo.device_details(device_id)
    return _subscription_out(device_id, details)


@router.put(
    "/{device_id}/subscription",
    response_model=DeviceSubscriptionOut,
    dependencies=[Depends(require_admin)],
    summary="Set or renew a device's subscription end date, installed_at and/or sim_expiry_date",
    response_description="The device's subscription status and dates after the change.",
)
async def set_subscription(
    device_id: str,
    payload: DeviceSubscriptionUpdate = DeviceSubscriptionUpdate(),
    admin: AuthenticatedUser = Depends(require_admin),
    repo: LocationRepository = Depends(get_repository),
) -> DeviceSubscriptionOut:
    """
    Create or renew metering on `device_id`, and/or set its other dates.

    Works for any device id, reported or not yet -- the same reasoning as
    assigning a device to an account before its first fix. Takes effect
    immediately: a device already past the new date is hidden on the very
    next read. The change is appended to `GET
    /devices/{device_id}/subscription-history`, never overwritten silently.

    Onboarding a device -- the common case -- means calling this with no
    `subscription_end_date` at all: it then defaults to `DEFAULT_SUBSCRIPTION_TERM`
    (365 days) from today, whether this is the device's first subscription or
    a renewal starting fresh from now rather than from the old end date.
    `installed_at` and `sim_expiry_date` behave differently: they are plain
    edit fields, so omitting either leaves it exactly as stored.
    """
    end_date = payload.subscription_end_date or (
        datetime.now(timezone.utc) + DEFAULT_SUBSCRIPTION_TERM
    )
    await repo.set_device_subscription(
        device_id,
        end_date,
        installed_at=payload.installed_at,
        sim_expiry_date=payload.sim_expiry_date,
        changed_by=admin.id,
    )
    return _subscription_out(device_id, await repo.device_details(device_id))


@router.delete(
    "/{device_id}/subscription",
    response_model=DeviceSubscriptionOut,
    dependencies=[Depends(require_admin)],
    summary="Lift metering on a device entirely",
    response_description="The device, now unmetered. installed_at/sim_expiry_date are untouched.",
    responses={404: {"description": "This device has no subscription to clear."}},
)
async def clear_subscription(
    device_id: str,
    admin: AuthenticatedUser = Depends(require_admin),
    repo: LocationRepository = Depends(get_repository),
) -> DeviceSubscriptionOut:
    """
    Back to unmetered -- the device never expires until put on a subscription
    again. Only `subscription_end_date` is cleared; `installed_at` and
    `sim_expiry_date` are unrelated facts about the device and are left as
    they were.
    """
    cleared = await repo.clear_device_subscription(device_id, changed_by=admin.id)
    if not cleared:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id} has no subscription to clear",
        )
    return _subscription_out(device_id, await repo.device_details(device_id))


@router.get(
    "/{device_id}/subscription-history",
    response_model=list[DeviceSubscriptionHistoryOut],
    dependencies=[Depends(require_admin)],
    summary="Renewal history for a device's subscription",
    response_description="Every change to subscription_end_date, newest first.",
)
async def subscription_history(
    device_id: str, repo: LocationRepository = Depends(get_repository)
) -> list[DeviceSubscriptionHistoryOut]:
    """
    Every change ever made to this device's `subscription_end_date` --
    who made it, and what the date moved from and to. Empty, not 404, for a
    device that has never been metered; there is nothing wrong with that
    device, it simply has no history yet.
    """
    return await repo.device_subscription_history(device_id)
