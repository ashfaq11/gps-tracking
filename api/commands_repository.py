"""
Device commands: engine cut-off and restore, sent over the tracker's own
connection by the gateway.

The API only queues a command here. Delivery, and the safety rules that must
hold at the moment of delivery (cut only while the ignition is off, expiry,
one gateway per command), live in sql/schema.sql's claim_device_commands(),
which the gateway calls. The in-memory implementation has no gateway behind
it, so its commands simply stay queued until they expire.
"""

from datetime import datetime, timedelta, timezone
from typing import Protocol

from .schemas import DeviceCommandOut, RelayStateOut

# The PT06 LITE manual's SMS commands; GT06-family trackers accept the same
# text over their GPRS connection as a 0x80 packet.
RELAY_COMMANDS = {"cut": "RELAY,1#", "restore": "RELAY,0#"}

# How long a command may wait for the tracker to come online. Past this it is
# never delivered: nobody expects an engine to stop hours after they asked.
COMMAND_TTL = timedelta(minutes=5)


class CommandRepository(Protocol):
    async def create_relay_command(
        self, device_id: str, action: str, requested_by: int | None, requester: str | None = None
    ) -> DeviceCommandOut:
        """Queue a command, superseding any still-queued one for the device.
        `requester` is the username for the record (Postgres reads it from
        users by `requested_by` instead)."""
        ...

    async def list_commands(self, device_id: str, limit: int = 20) -> list[DeviceCommandOut]:
        """Newest first. A queued command past its expiry reads as expired."""
        ...

    async def relay_state(self, device_id: str, limit: int = 5) -> RelayStateOut:
        """Whether cut-off is switched on (off when never set), plus recent commands."""
        ...

    async def set_relay_enabled(
        self, device_id: str, enabled: bool, changed_by: int | None, requester: str | None = None
    ) -> RelayStateOut:
        """Switch cut-off on or off. Switching off also fails any cut still
        queued ('relay_disabled') -- a queued restore is left to go out."""
        ...


def _effective(command: DeviceCommandOut, now: datetime) -> DeviceCommandOut:
    if command.status == "queued" and command.expires_at <= now:
        return command.model_copy(update={"status": "expired"})
    return command


class InMemoryCommandRepository:
    def __init__(self) -> None:
        self._commands: list[DeviceCommandOut] = []
        self._next_id = 1
        # device_id -> (enabled, changed_at, changed_by username)
        self._relay: dict[str, tuple[bool, datetime, str | None]] = {}

    async def relay_state(self, device_id: str, limit: int = 5) -> RelayStateOut:
        enabled, changed_at, changed_by = self._relay.get(device_id, (False, None, None))
        return RelayStateOut(
            device_id=device_id,
            enabled=enabled,
            changed_at=changed_at,
            changed_by=changed_by,
            commands=await self.list_commands(device_id, limit),
        )

    async def set_relay_enabled(
        self, device_id: str, enabled: bool, changed_by: int | None, requester: str | None = None
    ) -> RelayStateOut:
        now = datetime.now(timezone.utc)
        self._relay[device_id] = (enabled, now, requester)
        if not enabled:
            self._commands = [
                c.model_copy(
                    update={"status": "failed", "error": "relay_disabled", "completed_at": now}
                )
                if c.device_id == device_id
                and c.action == "cut"
                and c.status == "queued"
                and c.expires_at > now
                else c
                for c in self._commands
            ]
        return await self.relay_state(device_id)

    async def create_relay_command(
        self, device_id: str, action: str, requested_by: int | None, requester: str | None = None
    ) -> DeviceCommandOut:
        now = datetime.now(timezone.utc)
        self._commands = [
            c.model_copy(update={"status": "superseded", "completed_at": now})
            if c.device_id == device_id and c.status == "queued" and c.expires_at > now
            else c
            for c in self._commands
        ]
        command = DeviceCommandOut(
            id=self._next_id,
            device_id=device_id,
            action=action,
            command=RELAY_COMMANDS[action],
            status="queued",
            requested_by=requester,
            requested_at=now,
            expires_at=now + COMMAND_TTL,
        )
        self._next_id += 1
        self._commands.append(command)
        return command

    async def list_commands(self, device_id: str, limit: int = 20) -> list[DeviceCommandOut]:
        now = datetime.now(timezone.utc)
        mine = [c for c in self._commands if c.device_id == device_id]
        return [_effective(c, now) for c in sorted(mine, key=lambda c: -c.id)[:limit]]

    def set_status(self, command_id: int, **changes) -> None:
        """Test hook standing in for the gateway."""
        self._commands = [
            c.model_copy(update=changes) if c.id == command_id else c for c in self._commands
        ]


_COLUMNS = """
    c.id, c.device_id, c.action, c.command,
    CASE WHEN c.status = 'queued' AND c.expires_at <= now() THEN 'expired'
         ELSE c.status END AS status,
    u.username AS requested_by,
    c.requested_at, c.expires_at, c.sent_at, c.completed_at, c.reply, c.error
"""


class PostgresCommandRepository:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def create_relay_command(
        self, device_id: str, action: str, requested_by: int | None, requester: str | None = None
    ) -> DeviceCommandOut:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE device_commands
                    SET status = 'superseded', completed_at = now()
                    WHERE device_id = $1 AND status = 'queued' AND expires_at > now()
                    """,
                    device_id,
                )
                command_id = await conn.fetchval(
                    """
                    INSERT INTO device_commands
                        (device_id, action, command, requested_by, expires_at)
                    VALUES ($1, $2, $3, $4, now() + $5::interval)
                    RETURNING id
                    """,
                    device_id,
                    action,
                    RELAY_COMMANDS[action],
                    requested_by,
                    COMMAND_TTL,
                )
                row = await conn.fetchrow(
                    f"""
                    SELECT {_COLUMNS}
                    FROM device_commands c LEFT JOIN users u ON u.id = c.requested_by
                    WHERE c.id = $1
                    """,
                    command_id,
                )
        return DeviceCommandOut(**dict(row))

    async def relay_state(self, device_id: str, limit: int = 5) -> RelayStateOut:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.enabled, r.changed_at, u.username AS changed_by
                FROM device_relay r LEFT JOIN users u ON u.id = r.changed_by
                WHERE r.device_id = $1
                """,
                device_id,
            )
        return RelayStateOut(
            device_id=device_id,
            enabled=bool(row and row["enabled"]),
            changed_at=row["changed_at"] if row else None,
            changed_by=row["changed_by"] if row else None,
            commands=await self.list_commands(device_id, limit),
        )

    async def set_relay_enabled(
        self, device_id: str, enabled: bool, changed_by: int | None, requester: str | None = None
    ) -> RelayStateOut:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO device_relay (device_id, enabled, changed_by, changed_at)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (device_id) DO UPDATE
                    SET enabled = EXCLUDED.enabled, changed_by = EXCLUDED.changed_by,
                        changed_at = now()
                    """,
                    device_id,
                    enabled,
                    changed_by,
                )
                if not enabled:
                    await conn.execute(
                        """
                        UPDATE device_commands
                        SET status = 'failed', error = 'relay_disabled', completed_at = now()
                        WHERE device_id = $1 AND action = 'cut' AND status = 'queued'
                          AND expires_at > now()
                        """,
                        device_id,
                    )
        return await self.relay_state(device_id)

    async def list_commands(self, device_id: str, limit: int = 20) -> list[DeviceCommandOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS}
                FROM device_commands c LEFT JOIN users u ON u.id = c.requested_by
                WHERE c.device_id = $1
                ORDER BY c.id DESC
                LIMIT $2
                """,
                device_id,
                limit,
            )
        return [DeviceCommandOut(**dict(r)) for r in rows]
