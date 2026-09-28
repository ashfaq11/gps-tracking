"""
The tracker allowlist: which IMEIs the gateway accepts at login, and the
unknown ones it refused, waiting for an admin.

The gateway reads and records these itself (sql/schema.sql's gateway_admit);
this module is the admin's side. In Postgres a tracker someone owns is
allowed by a trigger on user_devices -- the in-memory backend has no such
link, so its tests add trackers explicitly.
"""

from datetime import datetime, timezone
from typing import Protocol

from .schemas import AllowedTrackerOut, LoginAttemptOut


class TrackerRepository(Protocol):
    async def list_allowed(self) -> list[AllowedTrackerOut]:
        """Newest first."""
        ...

    async def allow(
        self, device_id: str, added_by: int | None, requester: str | None = None
    ) -> AllowedTrackerOut:
        """Allow a tracker (clearing any refused attempt). Idempotent: an
        already-allowed tracker is returned as it was."""
        ...

    async def disallow(self, device_id: str) -> bool:
        """False if it was not on the list."""
        ...

    async def list_attempts(self) -> list[LoginAttemptOut]:
        """Most recent attempt first."""
        ...

    async def dismiss_attempt(self, device_id: str) -> bool:
        ...


class InMemoryTrackerRepository:
    def __init__(self) -> None:
        self._allowed: dict[str, AllowedTrackerOut] = {}
        self._attempts: dict[str, LoginAttemptOut] = {}

    async def list_allowed(self) -> list[AllowedTrackerOut]:
        return sorted(self._allowed.values(), key=lambda t: t.added_at, reverse=True)

    async def allow(self, device_id, added_by, requester=None) -> AllowedTrackerOut:
        self._attempts.pop(device_id, None)
        if device_id not in self._allowed:
            self._allowed[device_id] = AllowedTrackerOut(
                device_id=device_id,
                added_at=datetime.now(timezone.utc),
                added_by=requester,
                source="admin",
            )
        return self._allowed[device_id]

    async def disallow(self, device_id: str) -> bool:
        return self._allowed.pop(device_id, None) is not None

    async def list_attempts(self) -> list[LoginAttemptOut]:
        return sorted(self._attempts.values(), key=lambda a: a.last_seen, reverse=True)

    async def dismiss_attempt(self, device_id: str) -> bool:
        return self._attempts.pop(device_id, None) is not None

    def record_attempt(self, device_id: str, peer: str | None = None) -> None:
        """Test hook standing in for the gateway's gateway_admit()."""
        now = datetime.now(timezone.utc)
        current = self._attempts.get(device_id)
        self._attempts[device_id] = LoginAttemptOut(
            device_id=device_id,
            first_seen=current.first_seen if current else now,
            last_seen=now,
            attempts=(current.attempts + 1) if current else 1,
            last_peer=peer,
        )


_ALLOWED_COLUMNS = """
    a.device_id, a.added_at, u.username AS added_by, a.source
"""


class PostgresTrackerRepository:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def list_allowed(self) -> list[AllowedTrackerOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_ALLOWED_COLUMNS}
                FROM device_allowlist a LEFT JOIN users u ON u.id = a.added_by
                ORDER BY a.added_at DESC, a.device_id
                """
            )
        return [AllowedTrackerOut(**dict(r)) for r in rows]

    async def allow(self, device_id, added_by, requester=None) -> AllowedTrackerOut:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # The insert trigger clears the attempt; a tracker that was
                # already allowed keeps its original row, so clear it here too.
                await conn.execute(
                    """
                    INSERT INTO device_allowlist (device_id, added_by, source)
                    VALUES ($1, $2, 'admin')
                    ON CONFLICT (device_id) DO NOTHING
                    """,
                    device_id,
                    added_by,
                )
                await conn.execute(
                    "DELETE FROM device_login_attempts WHERE device_id = $1", device_id
                )
                row = await conn.fetchrow(
                    f"""
                    SELECT {_ALLOWED_COLUMNS}
                    FROM device_allowlist a LEFT JOIN users u ON u.id = a.added_by
                    WHERE a.device_id = $1
                    """,
                    device_id,
                )
        return AllowedTrackerOut(**dict(row))

    async def disallow(self, device_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM device_allowlist WHERE device_id = $1", device_id
            )
        return result.endswith(" 1")

    async def list_attempts(self) -> list[LoginAttemptOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT device_id, first_seen, last_seen, attempts, last_peer
                FROM device_login_attempts
                ORDER BY last_seen DESC
                LIMIT 200
                """
            )
        return [LoginAttemptOut(**dict(r)) for r in rows]

    async def dismiss_attempt(self, device_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM device_login_attempts WHERE device_id = $1", device_id
            )
        return result.endswith(" 1")
