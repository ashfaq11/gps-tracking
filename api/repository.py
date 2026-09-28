"""
Data access, behind an interface.

The Postgres implementation is what runs in production; the in-memory one
lets the API (and its tests) run with no database at all.
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Protocol

from .reports import (
    HALT_THRESHOLD,
    MAX_PLAUSIBLE_SPEED_KMH,
    MAX_RUNNING_GAP,
    SPIKE_KMH,
    CLOCK_AHEAD_LIMIT,
    CLOCK_BEHIND_LIMIT,
    ReportFix,
    fix_time,
    summarize_device,
)
from .schemas import (
    DEFAULT_VEHICLE_ICON,
    DeviceOut,
    DeviceReport,
    DeviceSubscriptionHistoryOut,
    FixBucket,
    LocationIn,
    LocationOut,
    SubscriptionStatus,
    VehicleIcon,
)

_COLUMNS = (
    "id, device_id, latitude, longitude, speed_kmh, course_deg, "
    "gps_fixed, satellites, fixed_at, received_at, mcc, mnc, lac, cell_id, ignition"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _subscription_status(end_date: datetime | None, *, at: datetime | None = None) -> SubscriptionStatus:
    """No end date, or one still in the future, is active; a passed one is expired."""
    return "active" if (end_date is None or end_date > (at or _now())) else "expired"


def _halted_since(rows: list[LocationOut]) -> datetime | None:
    """
    When this device was last seen moving, or its very first fix if it has
    never moved -- the in-memory equivalent of the Postgres implementation's
    aggregate below. Meaningless (and ignored by callers) unless the latest
    fix itself reads 0 km/h; computed regardless so the two backends agree
    on exactly the same value from exactly the same input.
    """
    moving = [r.received_at for r in rows if (r.speed_kmh or 0) > 0]
    return max(moving) if moving else min(r.received_at for r in rows)


class DeviceDetails(NamedTuple):
    """
    One device's non-telemetry facts, assembled from two tables: billing
    state and name from device_subscriptions, marker shape from
    device_markers (see sql/schema.sql for why they are separate). Absence
    of a row in either means the field's own default -- unmetered, unnamed,
    or a car -- never a missing DeviceDetails.
    """

    subscription_end_date: datetime | None
    installed_at: datetime | None
    sim_expiry_date: datetime | None
    name: str | None
    icon: VehicleIcon
    secret_code: str | None = None


# Marks "this field was not sent" for set_device_profile, distinct from an
# explicit `None` -- which for `name` means "clear it back to the device id".
# A plain default of `None` could not tell the two apart.
_UNSET = object()


class SecretCodeTaken(Exception):
    """Raised by generate_secret_code when a caller-chosen code is already
    in use by a different device."""


class LocationRepository(Protocol):
    async def add(self, location: LocationIn) -> LocationOut: ...

    async def latest_for_device(self, device_id: str) -> LocationOut | None: ...

    async def latest_for_devices(
        self, device_ids: frozenset[str] | None = None
    ) -> dict[str, LocationOut]:
        """
        One round trip's worth of `latest_for_device`, for every id in
        `device_ids` (or every device that has ever reported, if None --
        same "no filter" meaning as `list_devices`/`summary` below). A
        device with no fixes, or a lapsed subscription, is simply absent
        from the result rather than mapped to None -- there is no reason
        for a caller polling a whole fleet to pay for N HTTP requests
        (and N round trips through this same query) when one query already
        groups by device_id.
        """
        ...

    async def history_for_device(
        self, device_id: str, limit: int, since: datetime | None, until: datetime | None = None
    ) -> list[LocationOut]: ...

    async def list_devices(self, device_ids: frozenset[str] | None = None) -> list[DeviceOut]: ...

    async def device_stub(self, device_id: str) -> DeviceOut: ...

    async def profiled_device_ids(self) -> frozenset[str]:
        """Every device_id with a name or marker icon on record, whether or
        not it has ever reported a position or been claimed by anyone --
        lets an admin-registered device (see device_stub) show up before
        either of those happens."""
        ...

    async def summary(
        self, active_minutes: int, recent_hours: int, device_ids: frozenset[str] | None = None
    ) -> dict: ...

    async def fixes_over_time(
        self, hours: int, bucket_minutes: int, device_ids: frozenset[str] | None = None
    ) -> list[FixBucket]: ...

    async def trip_report(
        self, since: datetime, until: datetime, device_ids: frozenset[str] | None = None
    ) -> list[DeviceReport]:
        """Per-vehicle trips in [since, until], by api/reports.py's rules --
        longest distance first. Lapsed subscriptions are left out, as in
        summary()."""
        ...

    async def ping(self) -> bool: ...

    # --- device subscriptions ---
    #
    # Billing state on the device, not the dashboard account -- see
    # sql/schema.sql's device_subscriptions block. No row (or a null
    # subscription_end_date within one) means unmetered.

    async def device_details(self, device_id: str) -> DeviceDetails: ...

    async def device_ignition(self, device_id: str) -> bool | None:
        """The device's current ignition (device_status); None if never reported."""
        ...

    async def set_device_subscription(
        self,
        device_id: str,
        end_date: datetime,
        *,
        installed_at: datetime | None = None,
        sim_expiry_date: datetime | None = None,
        changed_by: int | None = None,
    ) -> None: ...

    async def clear_device_subscription(
        self, device_id: str, *, changed_by: int | None = None
    ) -> bool: ...

    async def device_subscription_history(
        self, device_id: str
    ) -> list[DeviceSubscriptionHistoryOut]: ...

    # --- device profile (name, marker icon) ---
    #
    # Cosmetic only, stored in the same table since it is the same "facts
    # about a device_id, not tied to any fix" shape as the subscription
    # fields above -- see sql/schema.sql. Unlike those, any account that can
    # see the device may change these, not just an admin.

    async def set_device_profile(
        self, device_id: str, *, name: object = _UNSET, icon: object = _UNSET
    ) -> None: ...

    async def generate_secret_code(self, device_id: str, *, code: str | None = None) -> str:
        """
        Set device_id's secret code to `code`, or a random one if omitted,
        replacing whatever it had before. Raises SecretCodeTaken if `code`
        already belongs to a different device.
        """
        ...

    async def device_id_by_secret_code(self, secret_code: str) -> str | None: ...


class InMemoryLocationRepository:
    """Development and test backend. Not durable, not shared across workers."""

    def __init__(self):
        self._rows: list[LocationOut] = []
        self._next_id = 1
        # device_id -> {"subscription_end_date":, "installed_at":, "sim_expiry_date":, "name":, "secret_code":}
        self._device_subscriptions: dict[str, dict] = {}
        self._device_subscription_history: list[dict] = []
        self._next_sub_history_id = 1
        # A dedicated mapping, mirroring sql/schema.sql's device_markers table
        # rather than a field on _device_subscriptions -- same "separate,
        # purely cosmetic concern" reasoning as the real table.
        self._device_markers: dict[str, VehicleIcon] = {}
        # secret_code -> device_id mapping
        self._secret_codes: dict[str, str] = {}
        # device_id -> (ignition, ignition_changed_at), mirroring device_status.
        # Only ingest feeds it here; heartbeats reach Postgres alone.
        self._ignition: dict[str, tuple[bool, datetime]] = {}

    def _sub_end_date(self, device_id: str) -> datetime | None:
        return self._device_subscriptions.get(device_id, {}).get("subscription_end_date")

    def _sub_status(self, device_id: str) -> SubscriptionStatus:
        return _subscription_status(self._sub_end_date(device_id))

    def _sub_active(self, device_id: str) -> bool:
        return self._sub_status(device_id) == "active"

    async def add(self, location: LocationIn) -> LocationOut:
        # Ingest is never blocked by an expired subscription -- only the
        # read side (list/latest/history/stats) is. A lapsed customer keeps
        # accumulating data instead of losing it while unpaid.
        row = LocationOut(
            id=self._next_id,
            received_at=datetime.now(timezone.utc),
            **location.model_dump(),
        )
        self._next_id += 1
        self._rows.append(row)
        if row.ignition is not None:
            # What sql/schema.sql's record_ignition() does for the same row.
            current = self._ignition.get(row.device_id)
            if current is None or current[0] != row.ignition:
                self._ignition[row.device_id] = (row.ignition, row.received_at)
        return row

    async def latest_for_device(self, device_id: str) -> LocationOut | None:
        if not self._sub_active(device_id):
            return None
        rows = [r for r in self._rows if r.device_id == device_id]
        return max(rows, key=lambda r: (r.received_at, r.id)) if rows else None

    async def latest_for_devices(
        self, device_ids: frozenset[str] | None = None
    ) -> dict[str, LocationOut]:
        by_device: dict[str, LocationOut] = {}
        for row in self._rows:
            if device_ids is not None and row.device_id not in device_ids:
                continue
            if not self._sub_active(row.device_id):
                continue
            current = by_device.get(row.device_id)
            if current is None or (row.received_at, row.id) > (current.received_at, current.id):
                by_device[row.device_id] = row
        return by_device

    async def history_for_device(
        self, device_id: str, limit: int, since: datetime | None, until: datetime | None = None
    ) -> list[LocationOut]:
        if not self._sub_active(device_id):
            return []
        rows = [r for r in self._rows if r.device_id == device_id]
        if since is not None:
            rows = [r for r in rows if r.received_at >= since]
        if until is not None:
            rows = [r for r in rows if r.received_at <= until]
        rows.sort(key=lambda r: (r.received_at, r.id), reverse=True)
        return rows[:limit]

    async def list_devices(self, device_ids: frozenset[str] | None = None) -> list[DeviceOut]:
        by_device: dict[str, list[LocationOut]] = {}
        for row in self._rows:
            if device_ids is not None and row.device_id not in device_ids:
                continue
            by_device.setdefault(row.device_id, []).append(row)
        return sorted(
            (
                DeviceOut(
                    device_id=device_id,
                    last_seen=max(r.received_at for r in rows),
                    fix_count=len(rows),
                    subscription_end_date=self._sub_end_date(device_id),
                    subscription_status=self._sub_status(device_id),
                    installed_at=self._device_subscriptions.get(device_id, {}).get("installed_at"),
                    sim_expiry_date=self._device_subscriptions.get(device_id, {}).get(
                        "sim_expiry_date"
                    ),
                    name=self._device_subscriptions.get(device_id, {}).get("name"),
                    icon=self._device_markers.get(device_id, DEFAULT_VEHICLE_ICON),
                    halted_since=_halted_since(rows),
                    ignition=self._ignition.get(device_id, (None, None))[0],
                    ignition_changed_at=self._ignition.get(device_id, (None, None))[1],
                )
                for device_id, rows in by_device.items()
            ),
            key=lambda d: d.last_seen,
            reverse=True,
        )

    async def device_stub(self, device_id: str) -> DeviceOut:
        # A DeviceOut for a device this method itself would never surface --
        # it never looks at self._rows, deliberately, so it cannot be used
        # to fake "this device has reported" (routers/users.py's
        # claim_or_conflict relies on list_devices alone staying strict
        # about that). This exists only so an admin-named or admin-claimed
        # device that has not transmitted yet can still be shown somewhere,
        # with fix_count 0 and last_seen null saying plainly that it hasn't.
        details = await self.device_details(device_id)
        return DeviceOut(
            device_id=device_id,
            last_seen=None,
            fix_count=0,
            subscription_end_date=details.subscription_end_date,
            subscription_status=_subscription_status(details.subscription_end_date),
            installed_at=details.installed_at,
            sim_expiry_date=details.sim_expiry_date,
            name=details.name,
            icon=details.icon,
            halted_since=None,
        )

    async def profiled_device_ids(self) -> frozenset[str]:
        return frozenset(self._device_subscriptions) | frozenset(self._device_markers)

    async def summary(
        self, active_minutes: int, recent_hours: int, device_ids: frozenset[str] | None = None
    ) -> dict:
        now = datetime.now(timezone.utc)
        active_cutoff = now - timedelta(minutes=active_minutes)
        recent_cutoff = now - timedelta(hours=recent_hours)
        rows = self._rows if device_ids is None else [r for r in self._rows if r.device_id in device_ids]
        rows = [r for r in rows if self._sub_active(r.device_id)]
        devices = {r.device_id for r in rows}
        active = {r.device_id for r in rows if r.received_at >= active_cutoff}
        return {
            "devices_total": len(devices),
            "devices_active": len(active),
            "fixes_total": len(rows),
            "fixes_recent": sum(1 for r in rows if r.received_at >= recent_cutoff),
            "last_fix_at": max((r.received_at for r in rows), default=None),
        }

    async def fixes_over_time(
        self, hours: int, bucket_minutes: int, device_ids: frozenset[str] | None = None
    ) -> list[FixBucket]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        width = timedelta(minutes=bucket_minutes)
        rows = self._rows if device_ids is None else [r for r in self._rows if r.device_id in device_ids]
        rows = [r for r in rows if self._sub_active(r.device_id)]
        counts: dict[datetime, int] = {}
        for row in rows:
            if row.received_at < cutoff:
                continue
            offset = (row.received_at - cutoff) // width
            counts[cutoff + offset * width] = counts.get(cutoff + offset * width, 0) + 1
        return [FixBucket(bucket=b, fixes=c) for b, c in sorted(counts.items())]

    async def trip_report(
        self, since: datetime, until: datetime, device_ids: frozenset[str] | None = None
    ) -> list[DeviceReport]:
        by_device: dict[str, list[tuple[datetime, LocationOut]]] = {}
        for row in self._rows:
            if device_ids is not None and row.device_id not in device_ids:
                continue
            at = fix_time(row.fixed_at, row.received_at)
            if not self._sub_active(row.device_id) or not since <= at <= until:
                continue
            by_device.setdefault(row.device_id, []).append((at, row))
        reports = []
        for device_id, rows in by_device.items():
            rows.sort(key=lambda pair: (pair[0], pair[1].id))
            trips = summarize_device(
                [
                    ReportFix(r.latitude, r.longitude, r.speed_kmh, at, r.gps_fixed)
                    for at, r in rows
                ]
            )
            reports.append(DeviceReport(device_id=device_id, **vars(trips)))
        return sorted(reports, key=lambda r: (-r.distance_km, r.device_id))

    async def ping(self) -> bool:
        return True

    # --- device subscriptions ---

    async def device_ignition(self, device_id: str) -> bool | None:
        current = self._ignition.get(device_id)
        return current[0] if current else None

    async def device_details(self, device_id: str) -> DeviceDetails:
        row = self._device_subscriptions.get(device_id)
        icon = self._device_markers.get(device_id, DEFAULT_VEHICLE_ICON)
        if row is None:
            return DeviceDetails(None, None, None, None, icon, None)
        return DeviceDetails(
            subscription_end_date=row.get("subscription_end_date"),
            installed_at=row.get("installed_at"),
            sim_expiry_date=row.get("sim_expiry_date"),
            name=row.get("name"),
            icon=icon,
            secret_code=row.get("secret_code"),
        )

    async def set_device_profile(
        self, device_id: str, *, name: object = _UNSET, icon: object = _UNSET
    ) -> None:
        if name is not _UNSET:
            row = self._device_subscriptions.setdefault(device_id, {})
            row["name"] = name
        if icon is not _UNSET:
            self._device_markers[device_id] = icon  # type: ignore[assignment]

    async def set_device_subscription(
        self,
        device_id: str,
        end_date: datetime,
        *,
        installed_at: datetime | None = None,
        sim_expiry_date: datetime | None = None,
        changed_by: int | None = None,
    ) -> None:
        row = self._device_subscriptions.setdefault(device_id, {})
        previous = row.get("subscription_end_date")
        row["subscription_end_date"] = end_date
        # installed_at/sim_expiry_date are edit fields, not renewal fields:
        # omitting one (None) leaves whatever was already stored untouched.
        if installed_at is not None:
            row["installed_at"] = installed_at
        if sim_expiry_date is not None:
            row["sim_expiry_date"] = sim_expiry_date
        self._device_subscription_history.append(
            {
                "id": self._next_sub_history_id,
                "device_id": device_id,
                "previous_end_date": previous,
                "new_end_date": end_date,
                "changed_by": changed_by,
                "changed_at": _now(),
            }
        )
        self._next_sub_history_id += 1

    async def clear_device_subscription(
        self, device_id: str, *, changed_by: int | None = None
    ) -> bool:
        row = self._device_subscriptions.get(device_id)
        previous = row.get("subscription_end_date") if row else None
        if previous is None:
            return False
        # Only the billing date is cleared -- installed_at/sim_expiry_date
        # are unrelated facts about the device and survive lifting metering.
        row["subscription_end_date"] = None
        self._device_subscription_history.append(
            {
                "id": self._next_sub_history_id,
                "device_id": device_id,
                "previous_end_date": previous,
                "new_end_date": None,
                "changed_by": changed_by,
                "changed_at": _now(),
            }
        )
        self._next_sub_history_id += 1
        return True

    async def device_subscription_history(self, device_id: str) -> list[DeviceSubscriptionHistoryOut]:
        entries = [h for h in self._device_subscription_history if h["device_id"] == device_id]
        entries.sort(key=lambda h: h["changed_at"], reverse=True)
        # The in-memory backend keeps no reference to InMemoryUserRepository
        # (they are independent instances even in production, sharing only a
        # connection pool), so there is no username to resolve here.
        return [DeviceSubscriptionHistoryOut(**h, changed_by_username=None) for h in entries]

    async def generate_secret_code(self, device_id: str, *, code: str | None = None) -> str:
        import secrets

        if code is not None and self._secret_codes.get(code, device_id) != device_id:
            raise SecretCodeTaken(code)
        code = code or secrets.token_urlsafe(12)
        # Remove any existing code for this device
        self._secret_codes = {k: v for k, v in self._secret_codes.items() if v != device_id}
        self._secret_codes[code] = device_id
        row = self._device_subscriptions.setdefault(device_id, {})
        row["secret_code"] = code
        return code

    async def device_id_by_secret_code(self, secret_code: str) -> str | None:
        return self._secret_codes.get(secret_code)


class PostgresLocationRepository:
    def __init__(self, pool):
        self._pool = pool

    async def add(self, location: LocationIn) -> LocationOut:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO device_locations
                    (device_id, latitude, longitude, speed_kmh, course_deg,
                     gps_fixed, satellites, fixed_at, received_at,
                     mcc, mnc, lac, cell_id, ignition)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), $9, $10, $11, $12, $13)
                RETURNING {_COLUMNS}
                """,
                location.device_id,
                location.latitude,
                location.longitude,
                location.speed_kmh,
                location.course_deg,
                location.gps_fixed,
                location.satellites,
                location.fixed_at,
                location.mcc,
                location.mnc,
                location.lac,
                location.cell_id,
                location.ignition,
            )
        return LocationOut(**dict(row))

    async def latest_for_device(self, device_id: str) -> LocationOut | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_COLUMNS} FROM device_locations
                WHERE device_id = $1
                  AND NOT EXISTS (
                      SELECT 1 FROM device_subscriptions ds
                      WHERE ds.device_id = $1 AND ds.subscription_end_date <= now()
                  )
                ORDER BY received_at DESC, id DESC
                LIMIT 1
                """,
                device_id,
            )
        return LocationOut(**dict(row)) if row else None

    async def latest_for_devices(
        self, device_ids: frozenset[str] | None = None
    ) -> dict[str, LocationOut]:
        # DISTINCT ON (device_id), with the matching ORDER BY, is Postgres's
        # per-group "top 1 row" -- one query and one pass of the
        # (device_id, received_at DESC) index instead of N round trips
        # through latest_for_device, one per device, the way a caller
        # polling a whole fleet otherwise has to.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT ON (dl.device_id) {_COLUMNS} FROM device_locations dl
                WHERE ($1::text[] IS NULL OR dl.device_id = ANY($1))
                  AND NOT EXISTS (
                      SELECT 1 FROM device_subscriptions ds
                      WHERE ds.device_id = dl.device_id AND ds.subscription_end_date <= now()
                  )
                ORDER BY dl.device_id, dl.received_at DESC, dl.id DESC
                """,
                list(device_ids) if device_ids is not None else None,
            )
        return {row["device_id"]: LocationOut(**dict(row)) for row in rows}

    async def history_for_device(
        self, device_id: str, limit: int, since: datetime | None, until: datetime | None = None
    ) -> list[LocationOut]:
        async with self._pool.acquire() as conn:
            # Both bounds are inclusive and both are optional, so one query
            # serves an open window, a half-open one and a closed range. The
            # (device_id, received_at DESC) index covers the range scan.
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS} FROM device_locations
                WHERE device_id = $1
                  AND ($2::timestamptz IS NULL OR received_at >= $2)
                  AND ($3::timestamptz IS NULL OR received_at <= $3)
                  AND NOT EXISTS (
                      SELECT 1 FROM device_subscriptions ds
                      WHERE ds.device_id = $1 AND ds.subscription_end_date <= now()
                  )
                ORDER BY received_at DESC, id DESC
                LIMIT $4
                """,
                device_id,
                since,
                until,
                limit,
            )
        return [LocationOut(**dict(r)) for r in rows]

    async def list_devices(self, device_ids: frozenset[str] | None = None) -> list[DeviceOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT dl.device_id,
                       MAX(dl.received_at)                AS last_seen,
                       COUNT(*)                             AS fix_count,
                       MAX(ds.subscription_end_date)        AS subscription_end_date,
                       MAX(ds.installed_at)                 AS installed_at,
                       MAX(ds.sim_expiry_date)              AS sim_expiry_date,
                       MAX(ds.name)                         AS name,
                       COALESCE(MAX(dm.marker_code), 'car') AS icon,
                       -- Last seen moving, or the very first fix if it has
                       -- never moved -- one aggregate alongside the others
                       -- already in this GROUP BY, not a second round trip.
                       COALESCE(
                           MAX(dl.received_at) FILTER (WHERE dl.speed_kmh > 0),
                           MIN(dl.received_at)
                       )                                     AS halted_since,
                       -- At most one device_status row per device, so these
                       -- aggregates only unwrap it for the GROUP BY.
                       bool_or(st.ignition)                 AS ignition,
                       MAX(st.ignition_changed_at)          AS ignition_changed_at
                FROM device_locations dl
                LEFT JOIN device_subscriptions ds ON ds.device_id = dl.device_id
                LEFT JOIN device_markers dm ON dm.device_id = dl.device_id
                LEFT JOIN device_status st ON st.device_id = dl.device_id
                WHERE ($1::text[] IS NULL OR dl.device_id = ANY($1::text[]))
                GROUP BY dl.device_id
                ORDER BY last_seen DESC
                """,
                list(device_ids) if device_ids is not None else None,
            )
        return [
            DeviceOut(
                device_id=r["device_id"],
                last_seen=r["last_seen"],
                fix_count=r["fix_count"],
                subscription_end_date=r["subscription_end_date"],
                subscription_status=_subscription_status(r["subscription_end_date"]),
                installed_at=r["installed_at"],
                sim_expiry_date=r["sim_expiry_date"],
                name=r["name"],
                icon=r["icon"],
                halted_since=r["halted_since"],
                ignition=r["ignition"],
                ignition_changed_at=r["ignition_changed_at"],
            )
            for r in rows
        ]

    async def device_stub(self, device_id: str) -> DeviceOut:
        # See the in-memory implementation's docstring: deliberately not a
        # query against device_locations, so it cannot be mistaken for "this
        # device has reported" by claim_or_conflict or anything else that
        # cares about that distinction.
        details = await self.device_details(device_id)
        return DeviceOut(
            device_id=device_id,
            last_seen=None,
            fix_count=0,
            subscription_end_date=details.subscription_end_date,
            subscription_status=_subscription_status(details.subscription_end_date),
            installed_at=details.installed_at,
            sim_expiry_date=details.sim_expiry_date,
            name=details.name,
            icon=details.icon,
            halted_since=None,
        )

    async def profiled_device_ids(self) -> frozenset[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT device_id FROM device_subscriptions "
                "UNION SELECT device_id FROM device_markers"
            )
        return frozenset(r["device_id"] for r in rows)

    async def summary(
        self, active_minutes: int, recent_hours: int, device_ids: frozenset[str] | None = None
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    count(DISTINCT device_id)                                    AS devices_total,
                    count(DISTINCT device_id) FILTER (
                        WHERE received_at >= now() - make_interval(mins => $1)
                    )                                                            AS devices_active,
                    count(*)                                                     AS fixes_total,
                    count(*) FILTER (
                        WHERE received_at >= now() - make_interval(hours => $2)
                    )                                                            AS fixes_recent,
                    max(received_at)                                             AS last_fix_at
                FROM device_locations
                WHERE ($3::text[] IS NULL OR device_id = ANY($3::text[]))
                  AND NOT EXISTS (
                      SELECT 1 FROM device_subscriptions ds
                      WHERE ds.device_id = device_locations.device_id
                        AND ds.subscription_end_date <= now()
                  )
                """,
                active_minutes,
                recent_hours,
                list(device_ids) if device_ids is not None else None,
            )
        return dict(row)

    async def fixes_over_time(
        self, hours: int, bucket_minutes: int, device_ids: frozenset[str] | None = None
    ) -> list[FixBucket]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT to_timestamp(
                           floor(extract(epoch FROM received_at) / ($2 * 60)) * ($2 * 60)
                       ) AS bucket,
                       count(*) AS fixes
                FROM device_locations
                WHERE received_at >= now() - make_interval(hours => $1)
                  AND ($3::text[] IS NULL OR device_id = ANY($3::text[]))
                  AND NOT EXISTS (
                      SELECT 1 FROM device_subscriptions ds
                      WHERE ds.device_id = device_locations.device_id
                        AND ds.subscription_end_date <= now()
                  )
                GROUP BY 1
                ORDER BY 1
                """,
                hours,
                bucket_minutes,
                list(device_ids) if device_ids is not None else None,
            )
        return [FixBucket(**dict(r)) for r in rows]

    async def trip_report(
        self, since: datetime, until: datetime, device_ids: frozenset[str] | None = None
    ) -> list[DeviceReport]:
        # One pass, in the database: LAG pairs each fix with the one before
        # it for distance and running time, and a running count of moving
        # fixes numbers the stationary runs between them, so every run of
        # consecutive stopped fixes shares a `grp`. Same rules, one for one,
        # as api/reports.py's summarize_device -- tests compare the two.
        #
        # `at` is reports.fix_time: the tracker's clock unless it is plainly
        # wrong. The received_at bounds in `base` are what that rule allows
        # (a fix at most CLOCK_AHEAD_LIMIT ahead of arrival, at most
        # CLOCK_BEHIND_LIMIT behind), so the (device_id, received_at) index
        # still narrows the scan before `at` does the exact filtering.
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH base AS (
                    SELECT device_id, id, latitude, longitude, speed_kmh, gps_fixed,
                           CASE WHEN fixed_at IS NULL
                                  OR fixed_at > received_at + $8::interval
                                  OR fixed_at < received_at - $9::interval
                                THEN received_at ELSE fixed_at
                           END                                        AS at
                    FROM device_locations dl
                    WHERE received_at >= $1::timestamptz - $8::interval
                      AND received_at <= $2::timestamptz + $9::interval
                      AND ($3::text[] IS NULL OR device_id = ANY($3::text[]))
                      AND NOT EXISTS (
                          SELECT 1 FROM device_subscriptions ds
                          WHERE ds.device_id = dl.device_id AND ds.subscription_end_date <= now()
                      )
                ),
                f AS (
                    SELECT device_id, id, latitude, longitude, speed_kmh, at, gps_fixed,
                           COALESCE(speed_kmh, 0)                     AS speed,
                           LEAD(COALESCE(speed_kmh, 0)) OVER w        AS n_speed,
                           LAG(latitude) OVER w                       AS p_lat,
                           LAG(longitude) OVER w                      AS p_lon,
                           LAG(at) OVER w                             AS p_at,
                           LAG(COALESCE(speed_kmh, 0)) OVER w         AS p_speed
                    FROM base
                    WHERE at >= $1 AND at <= $2
                    WINDOW w AS (PARTITION BY device_id ORDER BY at, id)
                ),
                seg AS (
                    SELECT f.*,
                           CASE WHEN p_lat IS NULL THEN 0 ELSE
                               2 * 6371000 * asin(least(1, sqrt(
                                   power(sin(radians(latitude - p_lat) / 2), 2)
                                   + cos(radians(p_lat)) * cos(radians(latitude))
                                     * power(sin(radians(longitude - p_lon) / 2), 2)
                               )))
                           END                                        AS metres,
                           CASE WHEN p_speed > 0 AND at - p_at <= $5
                                THEN extract(epoch FROM at - p_at) ELSE 0
                           END                                        AS running_s,
                           count(*) FILTER (WHERE speed > 0) OVER (
                               PARTITION BY device_id ORDER BY at, id
                           )                                          AS grp
                    FROM f
                ),
                halts AS (
                    SELECT device_id, extract(epoch FROM max(at) - min(at)) AS secs
                    FROM seg
                    WHERE speed = 0
                    GROUP BY device_id, grp
                    HAVING count(*) >= 2 AND max(at) - min(at) >= $4
                ),
                halt_totals AS (
                    SELECT device_id, count(*) AS n, sum(secs) AS secs, max(secs) AS longest
                    FROM halts GROUP BY device_id
                )
                SELECT s.device_id,
                       sum(s.metres) / 1000                     AS distance_km,
                       -- Plausible readings only: a GPS lock, under the
                       -- ceiling, and not a spike above both neighbours
                       -- (a missing neighbour does not count against it).
                       max(s.speed_kmh) FILTER (
                           WHERE s.gps_fixed IS DISTINCT FROM false
                             AND s.speed_kmh <= $6
                             AND NOT (
                                 (s.p_speed IS NOT NULL OR s.n_speed IS NOT NULL)
                                 AND (s.p_speed IS NULL OR s.speed_kmh > s.p_speed + $7)
                                 AND (s.n_speed IS NULL OR s.speed_kmh > s.n_speed + $7)
                             )
                       )                                        AS max_speed_kmh,
                       sum(s.running_s) / 60                    AS running_minutes,
                       COALESCE(max(h.n), 0)                    AS halt_count,
                       COALESCE(max(h.secs), 0) / 60            AS halt_minutes,
                       COALESCE(max(h.longest), 0) / 60         AS longest_halt_minutes,
                       count(*)                                 AS fix_count,
                       min(s.at)                                AS first_fix_at,
                       max(s.at)                                AS last_fix_at
                FROM seg s
                LEFT JOIN halt_totals h ON h.device_id = s.device_id
                GROUP BY s.device_id
                ORDER BY distance_km DESC, s.device_id
                """,
                since,
                until,
                list(device_ids) if device_ids is not None else None,
                HALT_THRESHOLD,
                MAX_RUNNING_GAP,
                MAX_PLAUSIBLE_SPEED_KMH,
                SPIKE_KMH,
                CLOCK_AHEAD_LIMIT,
                CLOCK_BEHIND_LIMIT,
            )
        return [
            DeviceReport(
                device_id=r["device_id"],
                distance_km=float(r["distance_km"]),
                max_speed_kmh=r["max_speed_kmh"],
                running_minutes=float(r["running_minutes"]),
                halt_count=r["halt_count"],
                halt_minutes=float(r["halt_minutes"]),
                longest_halt_minutes=float(r["longest_halt_minutes"]),
                fix_count=r["fix_count"],
                first_fix_at=r["first_fix_at"],
                last_fix_at=r["last_fix_at"],
            )
            for r in rows
        ]

    async def ping(self) -> bool:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    # --- device subscriptions ---

    async def device_ignition(self, device_id: str) -> bool | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT ignition FROM device_status WHERE device_id = $1", device_id
            )

    async def device_details(self, device_id: str) -> DeviceDetails:
        async with self._pool.acquire() as conn:
            sub_row = await conn.fetchrow(
                "SELECT subscription_end_date, installed_at, sim_expiry_date, name, secret_code "
                "FROM device_subscriptions WHERE device_id = $1",
                device_id,
            )
            icon = await conn.fetchval(
                "SELECT marker_code FROM device_markers WHERE device_id = $1", device_id
            )
        if sub_row is None:
            return DeviceDetails(None, None, None, None, icon or DEFAULT_VEHICLE_ICON, None)
        return DeviceDetails(
            subscription_end_date=sub_row["subscription_end_date"],
            installed_at=sub_row["installed_at"],
            sim_expiry_date=sub_row["sim_expiry_date"],
            name=sub_row["name"],
            icon=icon or DEFAULT_VEHICLE_ICON,
            secret_code=sub_row["secret_code"],
        )

    async def set_device_profile(
        self, device_id: str, *, name: object = _UNSET, icon: object = _UNSET
    ) -> None:
        # Two independent tables, so two independent upserts -- each one
        # touched only when its field was actually sent. Unlike
        # set_device_subscription's installed_at/sim_expiry_date (which use
        # COALESCE because NULL there always means "leave alone"), name's
        # NULL is a legitimate value -- it clears it -- so "was it sent at
        # all" has to gate the whole statement, not just the value.
        async with self._pool.acquire() as conn:
            if name is not _UNSET:
                await conn.execute(
                    """
                    INSERT INTO device_subscriptions (device_id, name)
                    VALUES ($1, $2)
                    ON CONFLICT (device_id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
                    """,
                    device_id,
                    name,
                )
            if icon is not _UNSET:
                await conn.execute(
                    """
                    INSERT INTO device_markers (device_id, marker_code)
                    VALUES ($1, $2)
                    ON CONFLICT (device_id) DO UPDATE SET
                        marker_code = EXCLUDED.marker_code, updated_at = now()
                    """,
                    device_id,
                    icon,
                )

    async def set_device_subscription(
        self,
        device_id: str,
        end_date: datetime,
        *,
        installed_at: datetime | None = None,
        sim_expiry_date: datetime | None = None,
        changed_by: int | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchval(
                    "SELECT subscription_end_date FROM device_subscriptions WHERE device_id = $1",
                    device_id,
                )
                await conn.execute(
                    """
                    INSERT INTO device_subscriptions
                        (device_id, subscription_end_date, installed_at, sim_expiry_date)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (device_id)
                    DO UPDATE SET
                        subscription_end_date = EXCLUDED.subscription_end_date,
                        -- installed_at/sim_expiry_date are edit fields, not renewal
                        -- fields: a NULL here (omitted in the request) leaves the
                        -- stored value untouched rather than clearing it.
                        installed_at = COALESCE(EXCLUDED.installed_at, device_subscriptions.installed_at),
                        sim_expiry_date = COALESCE(
                            EXCLUDED.sim_expiry_date, device_subscriptions.sim_expiry_date
                        ),
                        updated_at = now()
                    """,
                    device_id,
                    end_date,
                    installed_at,
                    sim_expiry_date,
                )
                await conn.execute(
                    """
                    INSERT INTO device_subscription_history
                        (device_id, previous_end_date, new_end_date, changed_by)
                    VALUES ($1, $2, $3, $4)
                    """,
                    device_id,
                    previous,
                    end_date,
                    changed_by,
                )

    async def clear_device_subscription(
        self, device_id: str, *, changed_by: int | None = None
    ) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchval(
                    "SELECT subscription_end_date FROM device_subscriptions WHERE device_id = $1",
                    device_id,
                )
                if previous is None:
                    return False
                # UPDATE, not DELETE: installed_at/sim_expiry_date are
                # unrelated facts about the device and survive lifting
                # metering. A row with subscription_end_date NULL reads as
                # unmetered exactly like no row at all everywhere above.
                await conn.execute(
                    "UPDATE device_subscriptions SET subscription_end_date = NULL, updated_at = now() "
                    "WHERE device_id = $1",
                    device_id,
                )
                await conn.execute(
                    """
                    INSERT INTO device_subscription_history
                        (device_id, previous_end_date, new_end_date, changed_by)
                    VALUES ($1, $2, NULL, $3)
                    """,
                    device_id,
                    previous,
                    changed_by,
                )
                return True

    async def device_subscription_history(self, device_id: str) -> list[DeviceSubscriptionHistoryOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT h.id, h.device_id, h.previous_end_date, h.new_end_date,
                       h.changed_by, u.username AS changed_by_username, h.changed_at
                FROM device_subscription_history h
                LEFT JOIN users u ON u.id = h.changed_by
                WHERE h.device_id = $1
                ORDER BY h.changed_at DESC
                """,
                device_id,
            )
        return [DeviceSubscriptionHistoryOut(**dict(row)) for row in rows]

    async def generate_secret_code(self, device_id: str, *, code: str | None = None) -> str:
        import secrets

        import asyncpg

        code = code or secrets.token_urlsafe(12)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO device_subscriptions (device_id, secret_code)
                    VALUES ($1, $2)
                    ON CONFLICT (device_id) DO UPDATE SET secret_code = EXCLUDED.secret_code, updated_at = now()
                    """,
                    device_id,
                    code,
                )
        except asyncpg.UniqueViolationError:
            raise SecretCodeTaken(code) from None
        return code

    async def device_id_by_secret_code(self, secret_code: str) -> str | None:
        async with self._pool.acquire() as conn:
            device_id = await conn.fetchval(
                "SELECT device_id FROM device_subscriptions WHERE secret_code = $1",
                secret_code,
            )
        return device_id
