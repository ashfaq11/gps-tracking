"""
Device commands: engine cut-off and restore, and tracker settings (upload
interval, reading the settings back), sent over the tracker's own connection
by the gateway.

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
RELAY_ACTIONS = ("cut", "restore")
# Settings commands: `timer` is TIMER,T1,T2# (built from the request),
# `param` is PARAM#, whose reply lists the tracker's current settings.
TRACKER_ACTIONS = ("timer", "param")
PARAM_COMMAND = "PARAM#"


def timer_command(moving_s: int, parked_s: int) -> str:
    return f"TIMER,{moving_s},{parked_s}#"

# How long a command may wait for the tracker to come online. Past this it is
# never delivered: nobody expects an engine to stop hours after they asked.
COMMAND_TTL = timedelta(minutes=5)
# A settings change is harmless whenever it lands, so it may wait for a
# tracker that is offline (or parked, reporting every T2) far longer.
SETTINGS_TTL = timedelta(days=1)


def _group(action: str) -> tuple[str, ...]:
    """What a new command supersedes while still queued: a cut or restore
    replaces either (the newest intent for the relay wins); a settings
    command replaces only its own action -- changing the upload interval
    must not cancel a waiting engine restore, and reading the settings must
    not cancel a waiting interval change."""
    return RELAY_ACTIONS if action in RELAY_ACTIONS else (action,)


class CommandRepository(Protocol):
    async def create_relay_command(
        self, device_id: str, action: str, requested_by: int | None, requester: str | None = None
    ) -> DeviceCommandOut:
        """Queue a command, superseding any still-queued one for the device.
        `requester` is the username for the record (Postgres reads it from
        users by `requested_by` instead)."""
        ...

    async def create_tracker_command(
        self,
        device_id: str,
        action: str,
        command: str,
        requested_by: int | None,
        requester: str | None = None,
    ) -> DeviceCommandOut:
        """Queue a settings command (TRACKER_ACTIONS), superseding a
        still-queued one of the same action for the device."""
        ...

    async def list_commands(
        self, device_id: str, limit: int = 20, actions: tuple[str, ...] | None = None
    ) -> list[DeviceCommandOut]:
        """Newest first, optionally only these actions. A queued command past
        its expiry reads as expired."""
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
            commands=await self.list_commands(device_id, limit, RELAY_ACTIONS),
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
        return self._create(device_id, action, RELAY_COMMANDS[action], COMMAND_TTL, requester)

    async def create_tracker_command(
        self,
        device_id: str,
        action: str,
        command: str,
        requested_by: int | None,
        requester: str | None = None,
    ) -> DeviceCommandOut:
        return self._create(device_id, action, command, SETTINGS_TTL, requester)

    def _create(
        self, device_id: str, action: str, text: str, ttl: timedelta, requester: str | None
    ) -> DeviceCommandOut:
        now = datetime.now(timezone.utc)
        group = _group(action)
        self._commands = [
            c.model_copy(update={"status": "superseded", "completed_at": now})
            if c.device_id == device_id
            and c.action in group
            and c.status == "queued"
            and c.expires_at > now
            else c
            for c in self._commands
        ]
        command = DeviceCommandOut(
            id=self._next_id,
            device_id=device_id,
            action=action,
            command=text,
            status="queued",
            requested_by=requester,
            requested_at=now,
            expires_at=now + ttl,
        )
        self._next_id += 1
        self._commands.append(command)
        return command

    async def list_commands(
        self, device_id: str, limit: int = 20, actions: tuple[str, ...] | None = None
    ) -> list[DeviceCommandOut]:
        now = datetime.now(timezone.utc)
        mine = [
            c
            for c in self._commands
            if c.device_id == device_id and (actions is None or c.action in actions)
        ]
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
        return await self._create(
            device_id, action, RELAY_COMMANDS[action], COMMAND_TTL, requested_by
        )

    async def create_tracker_command(
        self,
        device_id: str,
        action: str,
        command: str,
        requested_by: int | None,
        requester: str | None = None,
    ) -> DeviceCommandOut:
        return await self._create(device_id, action, command, SETTINGS_TTL, requested_by)

    async def _create(
        self, device_id: str, action: str, text: str, ttl: timedelta, requested_by: int | None
    ) -> DeviceCommandOut:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE device_commands
                    SET status = 'superseded', completed_at = now()
                    WHERE device_id = $1 AND status = 'queued' AND expires_at > now()
                      AND action = ANY($2::text[])
                    """,
                    device_id,
                    list(_group(action)),
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
                    text,
                    requested_by,
                    ttl,
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
            commands=await self.list_commands(device_id, limit, RELAY_ACTIONS),
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

    async def list_commands(
        self, device_id: str, limit: int = 20, actions: tuple[str, ...] | None = None
    ) -> list[DeviceCommandOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS}
                FROM device_commands c LEFT JOIN users u ON u.id = c.requested_by
                WHERE c.device_id = $1 AND ($3::text[] IS NULL OR c.action = ANY($3::text[]))
                ORDER BY c.id DESC
                LIMIT $2
                """,
                device_id,
                limit,
                list(actions) if actions is not None else None,
            )
        return [DeviceCommandOut(**dict(r)) for r in rows]
