"""
Announces "stopped" on the clock, not only when the next position arrives.

The trigger in sql/schema.sql can say a vehicle stopped only when a fix
lands, and a parked tracker reports every few minutes -- so the alert came
up to a whole parked interval after the three minutes were over.
`announce_due_stops()` finds the vehicles that have been stopped long enough
by looking at the time, and writes the same alert (and sends the same
`vehicle_motion` notification, hence the same push) the trigger would have;
this task just calls it every few seconds.

A task of the API process, started in `lifespan` next to the push listeners.
Several API processes are fine: the function takes an advisory lock, so only
one announces at a time.
"""

import asyncio
import logging

log = logging.getLogger(__name__)


async def run_stop_watcher(pool, interval_seconds: float) -> None:
    """Calls announce_due_stops() every `interval_seconds` until cancelled."""
    while True:
        try:
            announced = await pool.fetchval("SELECT announce_due_stops()")
            if announced:
                log.info("stopped alerts announced: %s", announced)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Storage hiccup: the next round tries again.
            log.exception("Could not check for stopped vehicles")
        await asyncio.sleep(interval_seconds)
