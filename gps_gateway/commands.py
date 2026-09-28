"""
Commands for trackers (engine cut-off / restore), queued by the API in
device_commands and delivered here over each tracker's own connection.

The API and the gateway never talk directly: the API inserts a row, the
database's trigger NOTIFYs 'device_command' with the device id, and the
gateway process holding that tracker's session claims and sends it. Every
safety rule (cut only while the ignition is off, expiry, one gateway per
command) is applied by sql/schema.sql's claim_device_commands() at the moment
of claiming -- see there.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# A reply is a failure when the tracker says so in words; anything else it
# answers (including "cut off after speed below 20km/h") is recorded as
# confirmed, with its text kept verbatim for whoever reads the history.
_FAILURE_WORDS = re.compile(r"fail|error|invalid|unknown", re.IGNORECASE)


def reply_is_failure(text: str) -> bool:
    return bool(_FAILURE_WORDS.search(text))


@dataclass(frozen=True)
class PendingCommand:
    id: int
    device_id: str
    action: str
    command: str


class CommandQueue:
    """No commands at all -- what the log sink runs with."""

    async def start(
        self,
        on_pending: Callable[[str], None],
        on_reconnect: Callable[[], None],
    ) -> None:
        pass

    async def claim(self, device_id: str) -> list[PendingCommand]:
        return []

    async def complete(
        self,
        command_id: int,
        device_id: str,
        *,
        ok: bool,
        reply: str | None = None,
        error: str | None = None,
    ) -> None:
        pass

    async def stop(self) -> None:
        pass


_COMPLETE = """
UPDATE device_commands
SET status = $3, reply = COALESCE($4, reply), error = $5, completed_at = now()
WHERE id = $1 AND device_id = $2
  AND (status = 'sent' OR (status = 'failed' AND error = 'no_reply'))
"""


class PostgresCommandQueue(CommandQueue):
    """
    One small pool for claiming and completing, plus one dedicated
    connection that LISTENs -- LISTEN state lives on a connection, so it
    cannot come from a pool. A dropped listener reconnects and then asks
    every connected session to check for commands, in case a NOTIFY was
    missed while it was down.
    """

    def __init__(self, dsn: str, statement_cache_size: int = 100, retry_s: float = 5.0):
        self._dsn = dsn
        self._statement_cache_size = statement_cache_size
        self._retry_s = retry_s
        self._pool = None
        self._listener_task: asyncio.Task | None = None

    async def start(self, on_pending, on_reconnect) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=1,
            max_size=2,
            statement_cache_size=self._statement_cache_size,
        )
        self._listener_task = asyncio.create_task(self._listen(on_pending, on_reconnect))

    async def _listen(self, on_pending, on_reconnect) -> None:
        import asyncpg

        first = True
        while True:
            conn = None
            try:
                conn = await asyncpg.connect(
                    dsn=self._dsn, statement_cache_size=self._statement_cache_size
                )
                closed = asyncio.Event()
                conn.add_termination_listener(lambda _c: closed.set())
                await conn.add_listener(
                    "device_command", lambda _c, _pid, _channel, device_id: on_pending(device_id)
                )
                log.info("Listening for device commands")
                if not first:
                    on_reconnect()
                first = False
                await closed.wait()
                log.warning("Device command listener connection closed; reconnecting")
            except asyncio.CancelledError:
                if conn is not None:
                    await conn.close()
                raise
            except Exception:
                log.exception("Device command listener failed; retrying in %ss", self._retry_s)
            await asyncio.sleep(self._retry_s)

    async def claim(self, device_id: str) -> list[PendingCommand]:
        rows = await self._pool.fetch(
            "SELECT id, device_id, action, command FROM claim_device_commands($1)", device_id
        )
        return [PendingCommand(r["id"], r["device_id"], r["action"], r["command"]) for r in rows]

    async def complete(self, command_id, device_id, *, ok, reply=None, error=None) -> None:
        await self._pool.execute(
            _COMPLETE, command_id, device_id, "confirmed" if ok else "failed", reply, error
        )

    async def stop(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pool is not None:
            await self._pool.close()


class SessionRegistry:
    """
    Which session holds which tracker, so a command reaches the connection
    it belongs to. A tracker that reconnects replaces its old session; the
    old one leaving later must not remove the new one.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, "Deliverable"] = {}

    def add(self, device_id: str, session: "Deliverable") -> None:
        self._sessions[device_id] = session

    def remove(self, device_id: str, session: "Deliverable") -> None:
        if self._sessions.get(device_id) is session:
            del self._sessions[device_id]

    def get(self, device_id: str) -> "Deliverable | None":
        return self._sessions.get(device_id)

    def notify(self, device_id: str) -> None:
        """A command was queued for `device_id` -- deliver it if we hold it."""
        session = self._sessions.get(device_id)
        if session is not None:
            session.schedule_delivery()

    def notify_all(self) -> None:
        for session in list(self._sessions.values()):
            session.schedule_delivery()


class Deliverable:
    """What the registry needs from a session (DeviceSession implements it)."""

    def schedule_delivery(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def build_command_queue(sink: str, pg_dsn: str, statement_cache_size: int) -> CommandQueue:
    if sink == "postgres":
        return PostgresCommandQueue(pg_dsn, statement_cache_size=statement_cache_size)
    return CommandQueue()
