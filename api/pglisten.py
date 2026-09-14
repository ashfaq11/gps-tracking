"""
Generic Postgres LISTEN/NOTIFY consumer.

Two features need the same shape around a LISTEN connection: `push.py`
(a `vehicle_motion` notification becomes a Web Push) and `live.py` (a
`device_fix` notification becomes a WebSocket broadcast). This is that shape,
parameterised by channel name and what to do with each payload, so the
reconnect/keepalive logic -- the fiddly part -- exists exactly once.
"""

import asyncio
import contextlib
import json
import logging
from typing import Awaitable, Callable

from .config import ApiConfig

log = logging.getLogger(__name__)

_KEEPALIVE_S = 60
_MAX_BACKOFF_S = 60

EventHandler = Callable[[dict], Awaitable[None]]


async def listen_forever(config: ApiConfig, channel: str, on_event: EventHandler) -> None:
    """
    Call `on_event(payload)` for every JSON notification on `channel`, for
    the life of the process. Cancel the returned task to stop it.

    A dedicated connection, not one borrowed from the request pool: `LISTEN`
    state lives on the connection itself, so it needs one that is never
    handed to another query and never recycled.

    A LISTEN connection that never sends a query has been observed, in
    practice, to stop delivering notifications after sitting idle for a
    while -- the connection stays visible and "idle" in `pg_stat_activity`,
    and the notify callback simply never fires again, silently, with no
    exception anywhere to catch. Whatever the exact cause (a NAT/proxy
    dropping a TCP stream Postgres itself still considers open is the usual
    suspect for this failure shape), the fix is the same either way: never
    let the connection go more than `_KEEPALIVE_S` without a round trip, and
    reconnect from scratch if anything about it fails.

    A bad payload or a raising handler is logged and skipped rather than
    killing the listener -- one malformed event or one unreachable
    destination must not stop every later one from being delivered.
    """
    import asyncpg

    backoff = 1.0
    while True:
        queue: asyncio.Queue[str] = asyncio.Queue()

        def on_notify(_connection, _pid, _channel, payload: str) -> None:
            queue.put_nowait(payload)

        conn = None
        try:
            conn = await asyncpg.connect(dsn=config.pg_dsn)
            await conn.add_listener(channel, on_notify)
            log.info("Listening for %s notifications", channel)
            backoff = 1.0

            while True:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    # Nothing arrived recently -- prove the connection is
                    # still actually alive rather than assuming things have
                    # simply been quiet.
                    await conn.fetchval("SELECT 1")
                    continue

                try:
                    event = json.loads(raw)
                except ValueError:
                    log.warning("Bad %s payload: %r", channel, raw)
                    continue
                try:
                    await on_event(event)
                except Exception:
                    log.exception("Handler failed for a %s event: %r", channel, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s listener connection lost; reconnecting in %.0fs", channel, backoff)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.remove_listener(channel, on_notify)
                with contextlib.suppress(Exception):
                    await conn.close()

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_S)
