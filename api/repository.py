"""
Data access, behind an interface.

The Postgres implementation is what runs in production; the in-memory one
lets the API (and its tests) run with no database at all.
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Protocol

from .schemas import (
    DEFAULT_VEHICLE_ICON,
    DeviceOut,
    DeviceSubscriptionHistoryOut,
    FixBucket,
    LocationIn,
    LocationOut,
    SubscriptionStatus,
    VehicleIcon,
)

_COLUMNS = (
    "id, device_id, latitude, longitude, speed_kmh, course_deg, "
    "gps_fixed, satellites, fixed_at, received_at"
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


# Marks "this field was not sent" for set_device_profile, distinct from an
# explicit `None` -- which for `name` means "clear it back to the device id".
# A plain default of `None` could not tell the two apart.
_UNSET = object()


class LocationRepository(Protocol):
    async def add(self, location: LocationIn) -> LocationOut: ...

    async def latest_for_device(self, device_id: str) -> LocationOut | None: ...

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

    async def ping(self) -> bool: ...

    # --- device subscriptions ---
    #
    # Billing state on the device, not the dashboard account -- see
    # sql/schema.sql's device_subscriptions block. No row (or a null
    # subscription_end_date within one) means unmetered.

    async def device_details(self, device_id: str) -> DeviceDetails: ...

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


class InMemoryLocationRepository:
    """Development and test backend. Not durable, not shared across workers."""

    def __init__(self):
        self._rows: list[LocationOut] = []
        self._next_id = 1
        # device_id -> {"subscription_end_date":, "installed_at":, "sim_expiry_date":, "name":}
        self._device_subscriptions: dict[str, dict] = {}
        self._device_subscription_history: list[dict] = []
        self._next_sub_history_id = 1
        # A dedicated mapping, mirroring sql/schema.sql's device_markers table
        # rather than a field on _device_subscriptions -- same "separate,
        # purely cosmetic concern" reasoning as the real table.
        self._device_markers: dict[str, VehicleIcon] = {}

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
        return row

    async def latest_for_device(self, device_id: str) -> LocationOut | None:
        if not self._sub_active(device_id):
            return None
        rows = [r for r in self._rows if r.device_id == device_id]
        return max(rows, key=lambda r: (r.received_at, r.id)) if rows else None

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

    async def ping(self) -> bool:
        return True

    # --- device subscriptions ---

    async def device_details(self, device_id: str) -> DeviceDetails:
        row = self._device_subscriptions.get(device_id)
        icon = self._device_markers.get(device_id, DEFAULT_VEHICLE_ICON)
        if row is None:
            return DeviceDetails(None, None, None, None, icon)
        return DeviceDetails(
            subscription_end_date=row.get("subscription_end_date"),
            installed_at=row.get("installed_at"),
            sim_expiry_date=row.get("sim_expiry_date"),
            name=row.get("name"),
            icon=icon,
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


class PostgresLocationRepository:
    def __init__(self, pool):
        self._pool = pool

    async def add(self, location: LocationIn) -> LocationOut:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO device_locations
                    (device_id, latitude, longitude, speed_kmh, course_deg,
                     gps_fixed, satellites, fixed_at, received_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
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
                       )                                     AS halted_since
                FROM device_locations dl
                LEFT JOIN device_subscriptions ds ON ds.device_id = dl.device_id
                LEFT JOIN device_markers dm ON dm.device_id = dl.device_id
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

    async def ping(self) -> bool:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    # --- device subscriptions ---

    async def device_details(self, device_id: str) -> DeviceDetails:
        async with self._pool.acquire() as conn:
            sub_row = await conn.fetchrow(
                "SELECT subscription_end_date, installed_at, sim_expiry_date, name "
                "FROM device_subscriptions WHERE device_id = $1",
                device_id,
            )
            icon = await conn.fetchval(
                "SELECT marker_code FROM device_markers WHERE device_id = $1", device_id
            )
        if sub_row is None:
            return DeviceDetails(None, None, None, None, icon or DEFAULT_VEHICLE_ICON)
        return DeviceDetails(
            subscription_end_date=sub_row["subscription_end_date"],
            installed_at=sub_row["installed_at"],
            sim_expiry_date=sub_row["sim_expiry_date"],
            name=sub_row["name"],
            icon=icon or DEFAULT_VEHICLE_ICON,
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
