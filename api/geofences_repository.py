"""
Geofences: areas drawn on the map, the vehicles each one watches, and the
report of every time one of them crossed an edge.

Crossings are *detected* by the `check_geofences` trigger in sql/schema.sql,
not here -- both position writers (the ingest endpoint and the gateway) have
to be covered, and only the database sees both. This module only stores the
geofences and reads the report back. The in-memory implementation therefore
never detects anything on its own; tests add report rows with
`record_event`.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from .schemas import GeofenceEventOut, GeofenceOut, GeoPoint

# Metres per degree of latitude (and of longitude at the equator).
_M_PER_DEG = 111_320.0


@dataclass(frozen=True)
class GeofenceShape:
    """A validated shape plus its bounding box, ready to store."""

    kind: str
    vertex_lats: list[float] | None
    vertex_lngs: list[float] | None
    center_lat: float | None
    center_lng: float | None
    radius_m: float | None
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float

    @staticmethod
    def polygon(points: Sequence[GeoPoint]) -> "GeofenceShape":
        lats = [p.lat for p in points]
        lngs = [p.lng for p in points]
        return GeofenceShape(
            kind="polygon",
            vertex_lats=lats,
            vertex_lngs=lngs,
            center_lat=None,
            center_lng=None,
            radius_m=None,
            min_lat=min(lats),
            max_lat=max(lats),
            min_lng=min(lngs),
            max_lng=max(lngs),
        )

    @staticmethod
    def circle(center: GeoPoint, radius_m: float) -> "GeofenceShape":
        dlat = radius_m / _M_PER_DEG
        # Guarded so a centre at a pole does not divide by zero.
        dlng = radius_m / (_M_PER_DEG * max(math.cos(math.radians(center.lat)), 1e-6))
        return GeofenceShape(
            kind="circle",
            vertex_lats=None,
            vertex_lngs=None,
            center_lat=center.lat,
            center_lng=center.lng,
            radius_m=radius_m,
            min_lat=center.lat - dlat,
            max_lat=center.lat + dlat,
            min_lng=center.lng - dlng,
            max_lng=center.lng + dlng,
        )


@dataclass(frozen=True)
class GeofenceFields:
    """Everything but the shape, for a create; for an update, None means
    "leave as it is"."""

    name: str | None = None
    device_ids: list[str] | None = None
    alert_on_exit: bool | None = None
    alert_on_enter: bool | None = None


class GeofenceRepository(Protocol):
    async def list_geofences(self, owner_id: int | None) -> list[GeofenceOut]:
        """Every geofence, or only `owner_id`'s."""
        ...

    async def get_geofence(self, geofence_id: int) -> GeofenceOut | None: ...

    async def create_geofence(
        self, owner_id: int, shape: GeofenceShape, fields: GeofenceFields
    ) -> GeofenceOut: ...

    async def update_geofence(
        self, geofence_id: int, shape: GeofenceShape | None, fields: GeofenceFields
    ) -> GeofenceOut | None:
        """A new shape forgets every vehicle's inside/outside state, as does
        adding a vehicle (only for that vehicle, which has none yet)."""
        ...

    async def delete_geofence(self, geofence_id: int) -> bool: ...

    async def list_events(
        self,
        *,
        owner_id: int | None,
        device_scope: frozenset[str] | None,
        geofence_id: int | None = None,
        device_id: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[GeofenceEventOut]:
        """
        Newest first. `owner_id` limits to that account's geofences and
        `device_scope` to those vehicles (None for either means no limit).
        `after_id` returns only rows newer than one already seen -- how the
        app polls for new alerts.
        """
        ...


def _points(lats: Sequence[float] | None, lngs: Sequence[float] | None) -> list[GeoPoint] | None:
    if lats is None or lngs is None:
        return None
    return [GeoPoint(lat=a, lng=b) for a, b in zip(lats, lngs)]


def _alerted(kind: str, alert_on_exit: bool, alert_on_enter: bool) -> bool:
    return alert_on_exit if kind == "exit" else alert_on_enter


class InMemoryGeofenceRepository:
    def __init__(self, usernames: dict[int, str] | None = None) -> None:
        self._fences: dict[int, dict] = {}
        self._events: list[dict] = []
        self._next_id = 1
        self._next_event_id = 1
        self._usernames = usernames if usernames is not None else {}

    def _out(self, row: dict) -> GeofenceOut:
        shape: GeofenceShape = row["shape"]
        return GeofenceOut(
            id=row["id"],
            name=row["name"],
            kind=shape.kind,
            vertices=_points(shape.vertex_lats, shape.vertex_lngs),
            center=(
                GeoPoint(lat=shape.center_lat, lng=shape.center_lng)
                if shape.center_lat is not None and shape.center_lng is not None
                else None
            ),
            radius_m=shape.radius_m,
            device_ids=sorted(row["device_ids"]),
            alert_on_exit=row["alert_on_exit"],
            alert_on_enter=row["alert_on_enter"],
            owner_id=row["owner_id"],
            owner_username=self._usernames.get(row["owner_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_geofences(self, owner_id: int | None) -> list[GeofenceOut]:
        rows = [r for r in self._fences.values() if owner_id is None or r["owner_id"] == owner_id]
        return [self._out(r) for r in sorted(rows, key=lambda r: r["name"].lower())]

    async def get_geofence(self, geofence_id: int) -> GeofenceOut | None:
        row = self._fences.get(geofence_id)
        return self._out(row) if row else None

    async def create_geofence(
        self, owner_id: int, shape: GeofenceShape, fields: GeofenceFields
    ) -> GeofenceOut:
        now = datetime.now(timezone.utc)
        row = {
            "id": self._next_id,
            "owner_id": owner_id,
            "name": fields.name or "",
            "shape": shape,
            "device_ids": set(fields.device_ids or []),
            "alert_on_exit": True if fields.alert_on_exit is None else fields.alert_on_exit,
            "alert_on_enter": bool(fields.alert_on_enter),
            "created_at": now,
            "updated_at": now,
        }
        self._fences[row["id"]] = row
        self._next_id += 1
        return self._out(row)

    async def update_geofence(
        self, geofence_id: int, shape: GeofenceShape | None, fields: GeofenceFields
    ) -> GeofenceOut | None:
        row = self._fences.get(geofence_id)
        if row is None:
            return None
        if shape is not None:
            row["shape"] = shape
        if fields.name is not None:
            row["name"] = fields.name
        if fields.device_ids is not None:
            row["device_ids"] = set(fields.device_ids)
        if fields.alert_on_exit is not None:
            row["alert_on_exit"] = fields.alert_on_exit
        if fields.alert_on_enter is not None:
            row["alert_on_enter"] = fields.alert_on_enter
        row["updated_at"] = datetime.now(timezone.utc)
        return self._out(row)

    async def delete_geofence(self, geofence_id: int) -> bool:
        if self._fences.pop(geofence_id, None) is None:
            return False
        self._events = [e for e in self._events if e["geofence_id"] != geofence_id]
        return True

    def record_event(
        self,
        geofence_id: int,
        device_id: str,
        kind: str,
        latitude: float = 0.0,
        longitude: float = 0.0,
        occurred_at: datetime | None = None,
    ) -> int:
        """Stand-in for the database trigger, for tests."""
        event_id = self._next_event_id
        self._next_event_id += 1
        self._events.append(
            {
                "id": event_id,
                "geofence_id": geofence_id,
                "device_id": device_id,
                "kind": kind,
                "latitude": latitude,
                "longitude": longitude,
                "occurred_at": occurred_at or datetime.now(timezone.utc),
            }
        )
        return event_id

    async def list_events(
        self,
        *,
        owner_id: int | None,
        device_scope: frozenset[str] | None,
        geofence_id: int | None = None,
        device_id: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[GeofenceEventOut]:
        out: list[GeofenceEventOut] = []
        for event in sorted(self._events, key=lambda e: (e["occurred_at"], e["id"]), reverse=True):
            fence = self._fences.get(event["geofence_id"])
            if fence is None:
                continue
            if owner_id is not None and fence["owner_id"] != owner_id:
                continue
            if device_scope is not None and event["device_id"] not in device_scope:
                continue
            if geofence_id is not None and event["geofence_id"] != geofence_id:
                continue
            if device_id is not None and event["device_id"] != device_id:
                continue
            if after_id is not None and event["id"] <= after_id:
                continue
            out.append(
                GeofenceEventOut(
                    id=event["id"],
                    geofence_id=event["geofence_id"],
                    geofence_name=fence["name"],
                    device_id=event["device_id"],
                    kind=event["kind"],
                    latitude=event["latitude"],
                    longitude=event["longitude"],
                    occurred_at=event["occurred_at"],
                    alerted=_alerted(event["kind"], fence["alert_on_exit"], fence["alert_on_enter"]),
                )
            )
            if len(out) >= limit:
                break
        return out


_FENCE_COLUMNS = """
    g.id, g.owner_id, u.username AS owner_username, g.name, g.kind,
    g.vertex_lats, g.vertex_lngs, g.center_lat, g.center_lng, g.radius_m,
    g.alert_on_exit, g.alert_on_enter, g.created_at, g.updated_at,
    COALESCE(
        (SELECT array_agg(d.device_id ORDER BY d.device_id)
         FROM geofence_devices d WHERE d.geofence_id = g.id),
        '{}'
    ) AS device_ids
"""


def _fence_out(row) -> GeofenceOut:
    center = (
        GeoPoint(lat=row["center_lat"], lng=row["center_lng"])
        if row["center_lat"] is not None and row["center_lng"] is not None
        else None
    )
    return GeofenceOut(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        vertices=_points(row["vertex_lats"], row["vertex_lngs"]),
        center=center,
        radius_m=row["radius_m"],
        device_ids=list(row["device_ids"]),
        alert_on_exit=row["alert_on_exit"],
        alert_on_enter=row["alert_on_enter"],
        owner_id=row["owner_id"],
        owner_username=row["owner_username"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresGeofenceRepository:
    def __init__(self, pool):
        self._pool = pool

    async def _fetch_one(self, conn, geofence_id: int) -> GeofenceOut | None:
        row = await conn.fetchrow(
            f"SELECT {_FENCE_COLUMNS} FROM geofences g "
            "LEFT JOIN users u ON u.id = g.owner_id WHERE g.id = $1",
            geofence_id,
        )
        return _fence_out(row) if row else None

    async def list_geofences(self, owner_id: int | None) -> list[GeofenceOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_FENCE_COLUMNS} FROM geofences g "
                "LEFT JOIN users u ON u.id = g.owner_id "
                "WHERE $1::bigint IS NULL OR g.owner_id = $1 "
                "ORDER BY lower(g.name), g.id",
                owner_id,
            )
        return [_fence_out(r) for r in rows]

    async def get_geofence(self, geofence_id: int) -> GeofenceOut | None:
        async with self._pool.acquire() as conn:
            return await self._fetch_one(conn, geofence_id)

    async def create_geofence(
        self, owner_id: int, shape: GeofenceShape, fields: GeofenceFields
    ) -> GeofenceOut:
        async with self._pool.acquire() as conn, conn.transaction():
            geofence_id = await conn.fetchval(
                """
                INSERT INTO geofences (
                    owner_id, name, kind, vertex_lats, vertex_lngs,
                    center_lat, center_lng, radius_m,
                    min_lat, max_lat, min_lng, max_lng,
                    alert_on_exit, alert_on_enter
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                RETURNING id
                """,
                owner_id,
                fields.name,
                shape.kind,
                shape.vertex_lats,
                shape.vertex_lngs,
                shape.center_lat,
                shape.center_lng,
                shape.radius_m,
                shape.min_lat,
                shape.max_lat,
                shape.min_lng,
                shape.max_lng,
                True if fields.alert_on_exit is None else fields.alert_on_exit,
                bool(fields.alert_on_enter),
            )
            await self._set_devices(conn, geofence_id, fields.device_ids or [])
            out = await self._fetch_one(conn, geofence_id)
        assert out is not None
        return out

    async def _set_devices(self, conn, geofence_id: int, device_ids: Sequence[str]) -> None:
        await conn.execute(
            "DELETE FROM geofence_devices WHERE geofence_id = $1 AND NOT (device_id = ANY($2::text[]))",
            geofence_id,
            list(device_ids),
        )
        # And the state of any vehicle no longer watched, so re-adding one
        # later starts afresh rather than from a months-old answer.
        await conn.execute(
            "DELETE FROM geofence_device_state "
            "WHERE geofence_id = $1 AND NOT (device_id = ANY($2::text[]))",
            geofence_id,
            list(device_ids),
        )
        await conn.execute(
            "INSERT INTO geofence_devices (geofence_id, device_id) "
            "SELECT $1, unnest($2::text[]) ON CONFLICT DO NOTHING",
            geofence_id,
            list(device_ids),
        )

    async def update_geofence(
        self, geofence_id: int, shape: GeofenceShape | None, fields: GeofenceFields
    ) -> GeofenceOut | None:
        async with self._pool.acquire() as conn, conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM geofences WHERE id = $1 FOR UPDATE", geofence_id
            )
            if not exists:
                return None
            await conn.execute(
                """
                UPDATE geofences SET
                    name = COALESCE($2, name),
                    alert_on_exit = COALESCE($3, alert_on_exit),
                    alert_on_enter = COALESCE($4, alert_on_enter),
                    updated_at = now()
                WHERE id = $1
                """,
                geofence_id,
                fields.name,
                fields.alert_on_exit,
                fields.alert_on_enter,
            )
            if shape is not None:
                await conn.execute(
                    """
                    UPDATE geofences SET
                        kind = $2, vertex_lats = $3, vertex_lngs = $4,
                        center_lat = $5, center_lng = $6, radius_m = $7,
                        min_lat = $8, max_lat = $9, min_lng = $10, max_lng = $11
                    WHERE id = $1
                    """,
                    geofence_id,
                    shape.kind,
                    shape.vertex_lats,
                    shape.vertex_lngs,
                    shape.center_lat,
                    shape.center_lng,
                    shape.radius_m,
                    shape.min_lat,
                    shape.max_lat,
                    shape.min_lng,
                    shape.max_lng,
                )
                await conn.execute(
                    "DELETE FROM geofence_device_state WHERE geofence_id = $1", geofence_id
                )
            if fields.device_ids is not None:
                await self._set_devices(conn, geofence_id, fields.device_ids)
            return await self._fetch_one(conn, geofence_id)

    async def delete_geofence(self, geofence_id: int) -> bool:
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM geofences WHERE id = $1 RETURNING id", geofence_id
            )
        return deleted is not None

    async def list_events(
        self,
        *,
        owner_id: int | None,
        device_scope: frozenset[str] | None,
        geofence_id: int | None = None,
        device_id: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[GeofenceEventOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.id, e.geofence_id, g.name AS geofence_name, e.device_id, e.kind,
                       e.latitude, e.longitude, e.occurred_at,
                       CASE WHEN e.kind = 'exit' THEN g.alert_on_exit
                            ELSE g.alert_on_enter END AS alerted
                FROM geofence_events e
                JOIN geofences g ON g.id = e.geofence_id
                WHERE ($1::bigint IS NULL OR g.owner_id = $1)
                  AND ($2::text[] IS NULL OR e.device_id = ANY($2::text[]))
                  AND ($3::bigint IS NULL OR e.geofence_id = $3)
                  AND ($4::text IS NULL OR e.device_id = $4)
                  AND ($5::bigint IS NULL OR e.id > $5)
                ORDER BY e.occurred_at DESC, e.id DESC
                LIMIT $6
                """,
                owner_id,
                sorted(device_scope) if device_scope is not None else None,
                geofence_id,
                device_id,
                after_id,
                limit,
            )
        return [GeofenceEventOut(**dict(r)) for r in rows]
