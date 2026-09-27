"""Request and response bodies."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .security import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH


class LocationIn(BaseModel):
    """
    A position reported over HTTP.

    Used by phone apps and by the minority of trackers that can POST JSON.
    GT06 hardware does not speak HTTP -- those devices reach the TCP gateway
    instead, and both paths land in the same table.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "868120303372449",
                    "latitude": 12.971598,
                    "longitude": 77.594566,
                    "speed_kmh": 42,
                    "course_deg": 15,
                    "gps_fixed": True,
                    "satellites": 9,
                    "fixed_at": "2026-09-06T03:28:59Z",
                    "mcc": 404,
                    "mnc": 45,
                    "lac": 4101,
                    "cell_id": 21635,
                }
            ]
        }
    }

    device_id: str = Field(
        min_length=1, max_length=64, description="IMEI for a tracker, or any stable client id."
    )
    latitude: float = Field(ge=-90, le=90, description="Decimal degrees; negative is south.")
    longitude: float = Field(ge=-180, le=180, description="Decimal degrees; negative is west.")
    speed_kmh: int = Field(default=0, ge=0, le=1000)
    course_deg: int = Field(default=0, ge=0, le=359, description="Heading, 0 = north.")
    gps_fixed: bool = Field(default=True, description="False when the fix is not trustworthy.")
    satellites: int = Field(default=0, ge=0, le=64)
    fixed_at: datetime | None = Field(
        default=None,
        description="When the device took the fix. Null if its clock was unset; "
        "fall back to received_at.",
    )
    # Serving cell (LBS). Optional, and meaningful only as a set -- the same
    # four fields the TCP gateway decodes from a GT06 packet.
    mcc: int | None = Field(default=None, ge=0, le=999, description="Mobile country code.")
    mnc: int | None = Field(default=None, ge=0, le=999, description="Mobile network code.")
    lac: int | None = Field(
        default=None, ge=0, le=0xFFFFFF, description="Location / tracking area code."
    )
    cell_id: int | None = Field(
        default=None, ge=0, le=2**36 - 1, description="Cell id (GSM CI, LTE ECI or 5G NCI)."
    )


class LocationOut(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": 2,
                    "device_id": "868120303372449",
                    "latitude": 12.981598,
                    "longitude": 77.594566,
                    "speed_kmh": 55,
                    "course_deg": 15,
                    "gps_fixed": True,
                    "satellites": 9,
                    "fixed_at": "2026-09-06T03:28:59Z",
                    "received_at": "2026-09-06T03:29:01.220Z",
                    "mcc": 404,
                    "mnc": 45,
                    "lac": 4101,
                    "cell_id": 21635,
                }
            ]
        }
    }

    id: int
    device_id: str
    latitude: float
    longitude: float
    speed_kmh: int | None = None
    course_deg: int | None = None
    gps_fixed: bool | None = None
    satellites: int | None = None
    fixed_at: datetime | None = None
    received_at: datetime
    mcc: int | None = None
    mnc: int | None = None
    lac: int | None = None
    cell_id: int | None = None


# Plain "active"/"expired" rather than a bare boolean -- the pairing every
# fleet-tracking dashboard already uses for this (Traccar, Samsara, Verizon
# Connect), so a label built from it needs no translation layer.
SubscriptionStatus = Literal["active", "expired"]

# The vehicle shapes the map can draw. A device with no explicit choice
# renders as a car -- the common case for a fleet -- rather than a shape-less
# dot, so an unconfigured fleet still reads as "vehicles on a map".
VehicleIcon = Literal["car", "bike", "auto", "van", "truck", "bus"]
DEFAULT_VEHICLE_ICON: VehicleIcon = "car"


class DeviceOut(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "868120303372449",
                    "last_seen": "2026-09-06T03:29:01.220Z",
                    "fix_count": 42,
                    "subscription_end_date": "2027-06-01T00:00:00Z",
                    "subscription_status": "active",
                    "installed_at": "2026-01-15T00:00:00Z",
                    "sim_expiry_date": "2027-01-15T00:00:00Z",
                    "name": "Delivery Van 3",
                    "icon": "van",
                    "owner_username": "priya",
                }
            ]
        }
    }

    device_id: str
    last_seen: datetime | None = Field(
        default=None,
        description="received_at of this device's most recent fix. Null for a device that "
        "has been named or claimed but has never actually reported a position -- see "
        "fix_count, which is 0 in that case.",
    )
    fix_count: int = Field(description="Total rows stored for this device.")
    subscription_end_date: datetime | None = Field(
        default=None,
        description="When this device's tracking access ends. Null if it was never "
        "put on a metered subscription -- unmetered devices never expire.",
    )
    subscription_status: SubscriptionStatus = Field(
        default="active",
        description="'expired' once subscription_end_date has passed. A device's "
        "location and history are then hidden from every viewer, admins "
        "included, until it is renewed.",
    )
    installed_at: datetime | None = Field(
        default=None, description="When the tracker was fitted and put into service. Informational."
    )
    sim_expiry_date: datetime | None = Field(
        default=None,
        description="When the tracker's cellular SIM / data plan runs out. Informational -- "
        "unlike subscription_end_date this never hides the device.",
    )
    name: str | None = Field(
        default=None,
        description="Operator-set label, e.g. a vehicle registration. Null if never named -- "
        "callers fall back to device_id themselves.",
    )
    icon: VehicleIcon = Field(
        default=DEFAULT_VEHICLE_ICON, description="Which vehicle shape this device draws on the map."
    )
    halted_since: datetime | None = Field(
        default=None,
        description="When this device was last seen moving (speed_kmh > 0), or its very first "
        "fix if it has never moved. Meaningful only when the latest fix reads 0 km/h -- a "
        "moving device's own halted_since is stale by definition and callers should ignore it. "
        "Derived from history, not client memory, so it is correct on a fresh page load and "
        "for a device nobody has been watching.",
    )
    owner_username: str | None = Field(
        default=None,
        description="Who has claimed this device (see device_claims). Null means unclaimed -- "
        "onboard it via PATCH/POST on this user, or POST /devices/claim.",
    )


class DeviceProfileUpdate(BaseModel):
    """
    A device's display name and/or map marker. Cosmetic only -- unlike
    DeviceSubscriptionUpdate, this never affects data visibility, and any
    account that can see the device may call it, not just an admin.

    Only the fields you send are changed. `name` accepts `null` (or `""`) to
    clear it back to showing the bare device id; there is no equivalent for
    `icon` -- every device always has one, so omit it to leave the current
    choice alone.
    """

    model_config = {"json_schema_extra": {"examples": [{"name": "Delivery Van 3", "icon": "van"}]}}

    name: str | None = Field(default=None, max_length=64)
    icon: VehicleIcon | None = Field(default=None, description="Omitted or null leaves it unchanged.")


class DeviceProfileOut(BaseModel):
    """The result of a profile change -- not DeviceOut, because this must
    work for a device id that has never reported a fix (see
    DeviceSubscriptionOut for the same reasoning)."""

    device_id: str
    name: str | None = None
    icon: VehicleIcon = DEFAULT_VEHICLE_ICON


class DeviceSubscriptionOut(BaseModel):
    """A device's current subscription status and other lifecycle dates."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "device_id": "868120303372449",
                    "subscription_end_date": "2027-06-01T00:00:00Z",
                    "subscription_status": "active",
                    "installed_at": "2026-01-15T00:00:00Z",
                    "sim_expiry_date": "2027-01-15T00:00:00Z",
                    "secret_code": "abc123def456",
                }
            ]
        }
    }

    device_id: str
    subscription_end_date: datetime | None = Field(
        default=None, description="Null means unmetered -- this device never expires."
    )
    subscription_status: SubscriptionStatus = Field(
        description="'expired' once subscription_end_date has passed."
    )
    installed_at: datetime | None = Field(
        default=None, description="When the tracker was fitted and put into service. Informational."
    )
    sim_expiry_date: datetime | None = Field(
        default=None,
        description="When the tracker's cellular SIM / data plan runs out. Informational -- "
        "unlike subscription_end_date this never hides the device.",
    )
    secret_code: str | None = Field(
        default=None,
        description="Unique code for anonymous live location access. Generate one with POST /devices/{device_id}/secret-code",
    )


class DeviceSubscriptionUpdate(BaseModel):
    """Sets or renews a device's subscription end date and/or its other dates. Admin only."""

    model_config = {
        "json_schema_extra": {"examples": [{"subscription_end_date": "2027-06-01T00:00:00Z"}]}
    }

    subscription_end_date: datetime | None = Field(
        default=None,
        description="The new end date. Omit it (or send `{}`) to onboard the device on the "
        "default term -- 365 days from today -- **this always overwrites**, even when "
        "omitted; it is a renewal field, not an edit field. To lift metering entirely, "
        "delete the subscription with DELETE instead of setting a far-future date here.",
    )
    installed_at: datetime | None = Field(
        default=None,
        description="When the tracker was fitted and put into service. Unlike "
        "subscription_end_date, omitting this leaves the stored value untouched -- "
        "there is no sensible default to fall back to.",
    )
    sim_expiry_date: datetime | None = Field(
        default=None,
        description="When the tracker's cellular SIM / data plan runs out. Omitting this "
        "also leaves the stored value untouched, same as installed_at.",
    )


class DeviceSubscriptionHistoryOut(BaseModel):
    """One change to a device's subscription_end_date, newest first."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": 1,
                    "device_id": "868120303372449",
                    "previous_end_date": "2026-06-01T00:00:00Z",
                    "new_end_date": "2027-06-01T00:00:00Z",
                    "changed_by": 1,
                    "changed_by_username": "admin",
                    "changed_at": "2026-09-09T10:00:00Z",
                }
            ]
        }
    }

    id: int
    device_id: str
    previous_end_date: datetime | None = Field(
        default=None, description="What the end date was before this change. Null if unmetered."
    )
    new_end_date: datetime | None = Field(
        default=None, description="What the end date became. Null if cleared back to unmetered."
    )
    changed_by: int | None = Field(
        default=None, description="Id of the admin who made the change, if that account still exists."
    )
    changed_by_username: str | None = None
    changed_at: datetime


class IngestAccepted(BaseModel):
    status: str = "accepted"
    device_id: str


class HealthOut(BaseModel):
    status: str
    backend: str
    database: str


class StatsSummary(BaseModel):
    """Headline numbers for the dashboard's stat tiles."""

    devices_total: int
    devices_active: int = Field(description="Devices seen within the active window.")
    active_window_minutes: int
    fixes_total: int
    fixes_recent: int = Field(description="Fixes received within the reporting window.")
    recent_window_hours: int
    last_fix_at: datetime | None = None


class FixBucket(BaseModel):
    """One point on the fixes-over-time chart."""

    bucket: datetime
    fixes: int


# --- dashboard accounts -----------------------------------------------------

Role = Literal["admin", "user"]

USERNAME_PATTERN = r"^[a-zA-Z0-9._-]+$"

# Deliberately loose. Neither is verified -- there is no mail or SMS provider
# wired up -- so these check the shape enough to catch a typo and no more.
# A stricter pattern would reject real addresses and real numbering plans
# while still not proving the person owns either.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MOBILE_PATTERN = r"^\+?[0-9][0-9 \-]{6,19}$"

Emailish = Annotated[
    str | None,
    Field(default=None, max_length=254, pattern=EMAIL_PATTERN, description="Not verified."),
]
Mobileish = Annotated[
    str | None,
    Field(default=None, max_length=20, pattern=MOBILE_PATTERN, description="Not verified."),
]


class UserOut(BaseModel):
    """
    An account as the dashboard sees it.

    There is no password field, in either direction on a read: a hash never
    leaves the database.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": 2,
                    "username": "dispatcher",
                    "full_name": "Ops Dispatcher",
                    "email": "ops@example.com",
                    "mobile": "+919876543210",
                    "role": "user",
                    "is_active": True,
                    "devices": ["868120303372000"],
                    "created_at": "2026-09-08T09:00:00Z",
                    "last_login_at": "2026-09-08T09:15:22Z",
                }
            ]
        }
    }

    id: int
    username: str
    full_name: str | None = None
    email: str | None = Field(
        default=None, description="Support and recovery handle. Unique across accounts."
    )
    mobile: str | None = Field(default=None, description="Contact number. Not verified.")
    role: Role
    is_active: bool = Field(description="False once an admin deactivates the account.")
    devices: list[str] = Field(
        default_factory=list,
        description="Devices this account may see. Empty for an admin, which sees all of them.",
    )
    created_at: datetime
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    """A new account. Only an admin may create one."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "username": "dispatcher",
                    "password": "a-good-passphrase",
                    "full_name": "Ops Dispatcher",
                    "role": "user",
                    "devices": ["868120303372000"],
                }
            ]
        }
    }

    username: str = Field(
        min_length=3,
        max_length=32,
        pattern=USERNAME_PATTERN,
        description="Letters, digits, dot, dash, underscore. Compared case-insensitively.",
    )
    password: str = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_BYTES,
        description="Stored only as a bcrypt hash.",
    )
    full_name: str | None = Field(default=None, max_length=120)
    email: Emailish = None
    mobile: Mobileish = None
    role: Role = "user"
    devices: list[str] = Field(
        default_factory=list,
        description="Ignored for an admin, which sees every device regardless.",
    )


class UserUpdate(BaseModel):
    """
    An edit. Every field is optional; only what is sent is changed.

    Username is absent deliberately -- it is what an account is known by in
    the audit trail, so renaming is a delete-and-recreate decision rather
    than an edit.
    """

    full_name: str | None = Field(default=None, max_length=120)
    email: Emailish = None
    mobile: Mobileish = None
    role: Role | None = None
    is_active: bool | None = Field(
        default=None, description="Set false to deactivate, true to restore."
    )
    devices: list[str] | None = Field(
        default=None, description="Replaces the whole assignment list when present."
    )
    password: str | None = Field(
        default=None,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_BYTES,
        description="Sets a new password. Existing sessions are signed out.",
    )


class SelfUpdate(BaseModel):
    """
    An account editing itself, via PATCH /auth/me -- deliberately narrower
    than UserUpdate: no `role`, `is_active` or `devices`, so this can never
    promote, reactivate or re-scope the caller's own account. Only the
    fields you send are changed.

    Setting a new password requires `current_password`, unlike an admin's
    UserUpdate -- an admin is already a trusted operator changing someone
    else's account; here the caller is proving they still are who the
    session claims before it does something as sensitive as a password
    change. Existing sessions are signed out afterward, this one included.
    """

    model_config = {
        "json_schema_extra": {"examples": [{"full_name": "Priya Sharma", "email": "priya@example.com"}]}
    }

    full_name: str | None = Field(default=None, max_length=120)
    email: Emailish = None
    mobile: Mobileish = None
    current_password: str | None = Field(
        default=None, description="Required only when `password` is set."
    )
    password: str | None = Field(
        default=None,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_BYTES,
        description="Sets a new password. Requires current_password. Existing sessions are "
        "signed out, this one included.",
    )


class SignupRequest(BaseModel):
    """
    Self-service signup: create an account and claim one device with it.

    The device is proof of nothing on its own -- there is no activation code
    -- so the rule is first claim wins, narrowed by two things the API
    enforces in `signup`: the device must already have reported a position
    (so an IMEI range cannot be claimed before the hardware ships), and a
    device that already has an owner can never be claimed again. An admin
    releases a device to hand it to a new owner.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "username": "priya",
                    "password": "a-good-passphrase",
                    "full_name": "Priya Sharma",
                    "email": "priya@example.com",
                    "mobile": "+919876543210",
                    "device_id": "868120303372449",
                }
            ]
        }
    }

    username: str = Field(
        min_length=3,
        max_length=32,
        pattern=USERNAME_PATTERN,
        description="Letters, digits, dot, dash, underscore. Compared case-insensitively.",
    )
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)
    full_name: str | None = Field(default=None, max_length=120)
    email: Emailish = None
    mobile: Mobileish = None
    device_id: str = Field(
        min_length=1, max_length=64, description="The IMEI printed on the tracker."
    )


class DeviceClaimRequest(BaseModel):
    """Add another device to the signed-in account. Same rules as signup."""

    model_config = {"json_schema_extra": {"examples": [{"device_id": "868120303372449"}]}}

    device_id: str = Field(min_length=1, max_length=64)


class LoginRequest(BaseModel):
    model_config = {
        "json_schema_extra": {"examples": [{"username": "admin", "password": "password"}]}
    }

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)


class LoginResponse(BaseModel):
    """The token to send back as `Authorization: Bearer <token>`."""

    token: str
    expires_at: datetime
    user: UserOut


class PushKeys(BaseModel):
    """The two keys `PushManager.subscribe()` returns, as the Web Push spec needs them."""

    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)


class PushSubscribeRequest(BaseModel):
    """The `PushSubscription` object returned by the browser, reshaped for the wire."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
                    "keys": {"p256dh": "BN4Gv...", "auth": "tBHI..."},
                }
            ]
        }
    }

    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushKeys


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


class VapidKeyOut(BaseModel):
    """Public only -- the private key never leaves the server."""

    public_key: str


class SecretCodeOut(BaseModel):
    """Generated secret code for anonymous device location access."""

    device_id: str
    secret_code: str = Field(description="Share this code to allow anonymous live location access.")


# Letters, digits, dash, underscore -- URL-safe with no encoding needed, so
# the code can be dropped straight into a shareable link's path segment.
SECRET_CODE_PATTERN = r"^[A-Za-z0-9_-]+$"


class SecretCodeUpdate(BaseModel):
    """
    Set a specific secret code rather than generating a random one -- e.g. a
    short human-memorable code for a route printed on a physical sign.
    Omit `secret_code` (or send `{}`) to auto-generate one instead, same as
    the plain POST.
    """

    model_config = {"json_schema_extra": {"examples": [{"secret_code": "route-42-bus"}]}}

    secret_code: str | None = Field(
        default=None,
        min_length=4,
        max_length=64,
        pattern=SECRET_CODE_PATTERN,
        description="A custom code. Letters, digits, dash, underscore. Must be unique across "
        "all devices. Omit to auto-generate a random one instead.",
    )


class DeviceLocationBySecretCode(BaseModel):
    """Vehicle and location accessible via secret code, no login required."""

    device_id: str
    name: str | None = Field(
        default=None, description="Vehicle display name, null if not set."
    )
    icon: VehicleIcon = Field(
        default=DEFAULT_VEHICLE_ICON, description="Vehicle shape for map display."
    )
    latitude: float
    longitude: float
    speed_kmh: int | None = None
    course_deg: int | None = None
    gps_fixed: bool | None = None
    satellites: int | None = None
    received_at: datetime = Field(description="When the latest position was received.")


# --- geofences ----------------------------------------------------------------

GeofenceKind = Literal["polygon", "circle"]
GeofenceEventKind = Literal["exit", "enter"]

MAX_GEOFENCE_VERTICES = 200
MIN_GEOFENCE_RADIUS_M = 20
MAX_GEOFENCE_RADIUS_M = 200_000


class GeoPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class GeofenceIn(BaseModel):
    """
    A geofence to create. `polygon` takes `vertices` (at least 3, in order;
    the last joins back to the first), `circle` takes `center` and
    `radius_m`. `device_ids` are the vehicles it watches -- each must be
    one the caller can see.
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "Depot",
                    "kind": "circle",
                    "center": {"lat": 16.3067, "lng": 80.4365},
                    "radius_m": 500,
                    "device_ids": ["868120303372222"],
                    "alert_on_exit": True,
                    "alert_on_enter": False,
                }
            ]
        }
    }

    name: str = Field(min_length=1, max_length=80)
    kind: GeofenceKind
    vertices: list[GeoPoint] | None = Field(default=None, max_length=MAX_GEOFENCE_VERTICES)
    center: GeoPoint | None = None
    radius_m: float | None = Field(
        default=None, ge=MIN_GEOFENCE_RADIUS_M, le=MAX_GEOFENCE_RADIUS_M
    )
    device_ids: list[str] = Field(default_factory=list, max_length=500)
    alert_on_exit: bool = True
    alert_on_enter: bool = False


class GeofenceUpdate(BaseModel):
    """
    Change a geofence. Only the fields sent are changed. Sending any shape
    field (`kind`, `vertices`, `center`, `radius_m`) replaces the shape as a
    whole and forgets every vehicle's inside/outside state, so the next
    position after a redraw starts afresh rather than reporting a crossing
    of a line that was never there.
    """

    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: GeofenceKind | None = None
    vertices: list[GeoPoint] | None = Field(default=None, max_length=MAX_GEOFENCE_VERTICES)
    center: GeoPoint | None = None
    radius_m: float | None = Field(
        default=None, ge=MIN_GEOFENCE_RADIUS_M, le=MAX_GEOFENCE_RADIUS_M
    )
    device_ids: list[str] | None = Field(default=None, max_length=500)
    alert_on_exit: bool | None = None
    alert_on_enter: bool | None = None


class GeofenceOut(BaseModel):
    id: int
    name: str
    kind: GeofenceKind
    vertices: list[GeoPoint] | None = None
    center: GeoPoint | None = None
    radius_m: float | None = None
    device_ids: list[str]
    alert_on_exit: bool
    alert_on_enter: bool
    owner_id: int
    owner_username: str | None = None
    created_at: datetime
    updated_at: datetime


class GeofenceEventOut(BaseModel):
    """One crossing of a geofence's edge -- a row of the geofence report."""

    id: int
    geofence_id: int
    geofence_name: str
    device_id: str
    kind: GeofenceEventKind
    latitude: float
    longitude: float
    occurred_at: datetime = Field(description="When the vehicle crossed.")
    alerted: bool = Field(
        description="Whether the geofence's settings made this crossing send an alert."
    )
