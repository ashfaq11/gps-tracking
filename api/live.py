"""
Live location: push every new fix to connected dashboards over WebSocket, the
instant it lands, instead of waiting for the next poll.

Same shape as push.py's vehicle-motion listener: a Postgres trigger
(`notify_new_fix` in sql/schema.sql) sees every insert from both writers --
this API's ingest endpoint and the TCP gateway -- and fires `pg_notify`; a
listener here (via pglisten.listen_forever) turns that into a message on
every WebSocket connection whose account can see the device.

Also serverless-incompatible for the same reason as push: the listener needs
a process that keeps running, which Vercel does not provide. A WebSocket
route itself is not servable there either -- Vercel's Python runtime speaks
plain request/response -- so live location, like push delivery, needs a host
that runs ASGI lifespan (see api/push.py's module docstring).
"""

import asyncio
import logging

from fastapi import WebSocket

from .config import ApiConfig
from .pglisten import listen_forever

log = logging.getLogger(__name__)

_CHANNEL = "device_fix"


class ConnectionManager:
    """
    Every open live-location socket, and which devices it may see.

    A plain list behind a lock rather than anything fancier -- this process
    is expected to hold at most a few dozen dashboard connections at once,
    not thousands, so O(n) fan-out per fix is not a real cost. `devices=None`
    means an admin: no filter, sees every device.
    """

    def __init__(self) -> None:
        self._connections: list[tuple[WebSocket, frozenset[str] | None]] = []
        self._lock = asyncio.Lock()

    async def register(self, socket: WebSocket, devices: frozenset[str] | None) -> None:
        async with self._lock:
            self._connections.append((socket, devices))

    async def unregister(self, socket: WebSocket) -> None:
        async with self._lock:
            self._connections = [c for c in self._connections if c[0] is not socket]

    @property
    def count(self) -> int:
        return len(self._connections)

    async def broadcast(self, fix: dict) -> None:
        device_id = fix.get("device_id")
        if not device_id:
            log.warning("%s event had no device_id: %r", _CHANNEL, fix)
            return
        async with self._lock:
            targets = [
                socket
                for socket, devices in self._connections
                if devices is None or device_id in devices
            ]
        for socket in targets:
            try:
                await socket.send_json(fix)
            except Exception:
                # A dead socket is discovered and cleaned up by its own
                # receive loop noticing the disconnect (see routers/live.py);
                # broadcasting to the others must not stop for one failure.
                log.debug("Could not send a live fix to a connection", exc_info=True)


async def run_fix_listener(config: ApiConfig, manager: ConnectionManager) -> None:
    """
    Forward `device_fix` notifications to every connection that can see the
    device. Runs for the life of the process; cancel the returned task to
    stop it.
    """
    await listen_forever(config, _CHANNEL, manager.broadcast)
