"""
Which trackers may log in: the allowlist in sql/schema.sql (device_allowlist,
checked and recorded by gateway_admit()).

GT06 has no authentication, so this cannot stop someone who knows an allowed
IMEI; it stops every IMEI nobody approved. Modes (GATEWAY_ALLOWLIST):
- enforce: an unknown IMEI's login is refused and the connection closed
- log:     everyone is admitted, unknown IMEIs are still recorded for review
- off:     no check at all (also what the log sink runs with -- no database)

An allowed IMEI is remembered for CACHE_S, so a tracker reconnecting every
few minutes (or a whole fleet after a network blip) costs no query. If the
database cannot be asked, the login is admitted with a warning: nothing can
be written or commanded without the database anyway, and refusing would
turn a database blip into every tracker dropping off.
"""

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

CACHE_S = 300.0
MODES = ("enforce", "log", "off")


class Admission:
    """Admits everyone -- mode 'off'."""

    async def admit(self, device_id: str, peer: str) -> bool:
        return True


class PostgresAdmission(Admission):
    def __init__(self, pool: Callable[[], object], mode: str = "enforce", cache_s: float = CACHE_S):
        if mode not in ("enforce", "log"):
            raise ValueError(f"PostgresAdmission mode must be 'enforce' or 'log', not {mode!r}")
        # A callable, so the pool can be one another component starts later.
        self._pool = pool
        self._mode = mode
        self._cache_s = cache_s
        self._allowed_until: dict[str, float] = {}

    async def admit(self, device_id: str, peer: str) -> bool:
        now = time.monotonic()
        if self._allowed_until.get(device_id, 0.0) > now:
            return True
        try:
            allowed = await self._pool().fetchval("SELECT gateway_admit($1, $2)", device_id, peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Allowlist check failed for %s; admitting", device_id)
            return True
        if allowed:
            self._allowed_until[device_id] = now + self._cache_s
            return True
        if self._mode == "log":
            log.warning("Unknown device %s (%s) admitted: GATEWAY_ALLOWLIST=log", device_id, peer)
            return True
        return False
