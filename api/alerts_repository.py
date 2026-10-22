"""
The alerts history: every notification a vehicle raised, kept (3 months).

Rows are written by Postgres triggers (sql/schema.sql: record_vehicle_alert),
not by the API -- the same shape as device_status -- so both writers are
covered. This repository only reads them, keeps each vehicle's overspeed
limit, and remembers how far each account has read. There is no way to
delete from here: the history is not something a screen can clear.

The in-memory implementation has no triggers behind it; tests (and nothing
else) put alerts in with `record`.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .schemas import AlertOut

DEFAULT_SPEED_LIMIT_KMH = 80
MIN_SPEED_LIMIT_KMH = 20
MAX_SPEED_LIMIT_KMH = 200

GEOFENCE_KINDS = ("geofence_enter", "geofence_exit")


@dataclass(frozen=True)
class AlertViewer:
    """Whose history this is: an admin sees everything; anyone else their own
    vehicles' alerts, and a geofence's only if they drew it (and may still use
    geofences)."""

    user_id: int
    is_admin: bool
    devices: frozenset[str]
    can_use_geofences: bool

    def sees(self, device_id: str, kind: str, owner_id: int | None) -> bool:
        if self.is_admin:
            return True
        if device_id not in self.devices:
            return False
        if kind in GEOFENCE_KINDS:
            return self.can_use_geofences and owner_id == self.user_id
        return True


class AlertRepository(Protocol):
    async def list_alerts(
        self,
        viewer: AlertViewer,
        *,
        device_id: str | None = None,
        kinds: tuple[str, ...] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        after_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AlertOut], int]:
        """Newest first: (one page, how many match in all)."""
        ...

    async def unread(self, viewer: AlertViewer) -> tuple[int, int]:
        """(alerts newer than the account's read mark, the read mark)."""
        ...

    async def mark_seen(self, viewer: AlertViewer, seen_id: int) -> int:
        """Move the read mark forward (never back); returns where it is now."""
        ...

    async def speed_limits(self, device_ids: frozenset[str] | None) -> dict[str, int]:
        """The limits that were set; a vehicle not in the answer has the default.
        None means every vehicle."""
        ...

    async def set_speed_limit(self, device_id: str, limit_kmh: int) -> None: ...


class InMemoryAlertRepository:
    def __init__(self) -> None:
        self._alerts: list[dict] = []
        self._next_id = 1
        self._limits: dict[str, int] = {}
        self._seen: dict[int, int] = {}

    def record(
        self,
        device_id: str,
        kind: str,
        occurred_at: datetime,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        speed_kmh: int | None = None,
        from_kmh: int | None = None,
        limit_kmh: int | None = None,
        geofence_id: int | None = None,
        geofence_name: str | None = None,
        owner_id: int | None = None,
    ) -> int:
        """What the database triggers do; for tests."""
        row = {
            "id": self._next_id,
            "device_id": device_id,
            "kind": kind,
            "occurred_at": occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc),
            "latitude": latitude,
            "longitude": longitude,
            "speed_kmh": speed_kmh,
            "from_kmh": from_kmh,
            "limit_kmh": limit_kmh,
            "geofence_id": geofence_id,
            "geofence_name": geofence_name,
            "owner_id": owner_id,
        }
        self._alerts.append(row)
        self._next_id += 1
        return row["id"]

    def _visible(self, viewer: AlertViewer) -> list[dict]:
        rows = [r for r in self._alerts if viewer.sees(r["device_id"], r["kind"], r["owner_id"])]
        return sorted(rows, key=lambda r: (r["occurred_at"], r["id"]), reverse=True)

    async def list_alerts(
        self, viewer, *, device_id=None, kinds=None, since=None, until=None, after_id=None, offset=0, limit=50
    ):
        rows = [
            r
            for r in self._visible(viewer)
            if (device_id is None or r["device_id"] == device_id)
            and (kinds is None or r["kind"] in kinds)
            and (since is None or r["occurred_at"] >= since)
            and (until is None or r["occurred_at"] <= until)
            and (after_id is None or r["id"] > after_id)
        ]
        page = rows[offset : offset + limit]
        return [AlertOut(**{k: v for k, v in r.items() if k != "owner_id"}) for r in page], len(rows)

    async def unread(self, viewer):
        seen = self._seen.get(viewer.user_id, 0)
        return sum(1 for r in self._visible(viewer) if r["id"] > seen), seen

    async def mark_seen(self, viewer, seen_id):
        self._seen[viewer.user_id] = max(self._seen.get(viewer.user_id, 0), seen_id)
        return self._seen[viewer.user_id]

    async def speed_limits(self, device_ids):
        return {d: v for d, v in self._limits.items() if device_ids is None or d in device_ids}

    async def set_speed_limit(self, device_id, limit_kmh):
        self._limits[device_id] = limit_kmh


_COLUMNS = (
    "id, device_id, kind, occurred_at, latitude, longitude, speed_kmh, from_kmh, "
    "limit_kmh, geofence_id, geofence_name"
)

# The viewer's slice of vehicle_alerts: $1 admin, $2 user id, $3 their vehicles,
# $4 may use geofences -- AlertViewer.sees, in SQL.
_VISIBLE = """
    ($1::boolean
     OR (device_id = ANY($3::text[])
         AND (kind NOT LIKE 'geofence\\_%' OR ($4::boolean AND owner_id = $2))))
"""


class PostgresAlertRepository:
    def __init__(self, pool) -> None:
        self._pool = pool

    @staticmethod
    def _who(viewer: AlertViewer):
        return viewer.is_admin, viewer.user_id, sorted(viewer.devices), viewer.can_use_geofences

    async def list_alerts(
        self, viewer, *, device_id=None, kinds=None, since=None, until=None, after_id=None, offset=0, limit=50
    ):
        where = f"""
            {_VISIBLE}
            AND ($5::text IS NULL OR device_id = $5)
            AND ($6::text[] IS NULL OR kind = ANY($6::text[]))
            AND ($7::timestamptz IS NULL OR occurred_at >= $7)
            AND ($8::timestamptz IS NULL OR occurred_at <= $8)
            AND ($9::bigint IS NULL OR id > $9)
        """
        args = (*self._who(viewer), device_id, list(kinds) if kinds else None, since, until, after_id)
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM vehicle_alerts WHERE {where}", *args)
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM vehicle_alerts WHERE {where}
                    ORDER BY occurred_at DESC, id DESC OFFSET $10 LIMIT $11""",
                *args,
                offset,
                limit,
            )
        return [AlertOut(**dict(r)) for r in rows], total

    async def unread(self, viewer):
        async with self._pool.acquire() as conn:
            seen = await conn.fetchval("SELECT seen_id FROM alert_reads WHERE user_id = $1", viewer.user_id) or 0
            count = await conn.fetchval(
                f"SELECT count(*) FROM vehicle_alerts WHERE {_VISIBLE} AND id > $5",
                *self._who(viewer),
                seen,
            )
        return count, seen

    async def mark_seen(self, viewer, seen_id):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO alert_reads (user_id, seen_id) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                    SET seen_id = GREATEST(alert_reads.seen_id, EXCLUDED.seen_id), updated_at = now()
                RETURNING seen_id
                """,
                viewer.user_id,
                seen_id,
            )

    async def speed_limits(self, device_ids):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT device_id, limit_kmh FROM device_speed_limits "
                "WHERE $1::text[] IS NULL OR device_id = ANY($1::text[])",
                sorted(device_ids) if device_ids is not None else None,
            )
        return {r["device_id"]: r["limit_kmh"] for r in rows}

    async def set_speed_limit(self, device_id, limit_kmh):
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO device_speed_limits (device_id, limit_kmh) VALUES ($1, $2)
                ON CONFLICT (device_id) DO UPDATE SET limit_kmh = EXCLUDED.limit_kmh, updated_at = now()
                """,
                device_id,
                limit_kmh,
            )
