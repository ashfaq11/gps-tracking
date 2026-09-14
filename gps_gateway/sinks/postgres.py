"""
Postgres sink.

The schema is NOT created here -- run `sql/schema.sql` once before starting
the gateway. Keeping DDL out of the hot path means the service needs no
schema-modifying privileges at runtime, and schema changes stay reviewable
in version control instead of happening implicitly on process start.
"""

import asyncio
import logging

from ..models import LocationEvent
from .base import Sink

log = logging.getLogger(__name__)

_COLUMNS = (
    "device_id",
    "latitude",
    "longitude",
    "speed_kmh",
    "course_deg",
    "gps_fixed",
    "satellites",
    "fixed_at",
    "received_at",
)

_INSERT = f"""
INSERT INTO device_locations ({", ".join(_COLUMNS)})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

# SQLSTATE classes meaning "Postgres rejected this data" -- 22 data
# exception, 23 integrity violation -- as opposed to "Postgres is unreachable
# or refusing work", where retrying row by row only repeats the same failure.
_ROW_ERROR_CLASSES = ("22", "23")

# What asyncpg raises when a record cannot even be encoded client-side (a
# speed that does not fit INT, say). These carry no SQLSTATE, but they are
# just as much about one row as a server-side rejection is.
_ENCODING_ERRORS = (OverflowError, ValueError, TypeError)


class SchemaMissingError(RuntimeError):
    pass


def is_row_error(exc: BaseException) -> bool:
    if isinstance(exc, _ENCODING_ERRORS):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate[:2] in _ROW_ERROR_CLASSES


def _record(event: LocationEvent) -> tuple:
    return (
        event.device_id,
        event.latitude,
        event.longitude,
        event.speed_kmh,
        event.course_deg,
        event.gps_fixed,
        event.satellites,
        event.fixed_at,
        event.received_at,
    )


class PostgresSink(Sink):
    def __init__(
        self,
        dsn: str,
        min_size: int = 1,
        max_size: int = 5,
        statement_cache_size: int = 100,
    ):
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._statement_cache_size = statement_cache_size
        self._pool = None

    async def start(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            statement_cache_size=self._statement_cache_size,
        )
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval("SELECT to_regclass('public.device_locations')")
        if exists is None:
            await self._pool.close()
            self._pool = None
            raise SchemaMissingError(
                "Table 'device_locations' is missing. Create it first:\n"
                "    psql \"$PG_DSN\" -f sql/schema.sql"
            )
        log.info("Postgres pool ready")

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("PostgresSink.start() was not awaited")
        return self._pool

    async def publish(self, event: LocationEvent) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute(_INSERT, *_record(event))

    async def publish_many(self, events: list[LocationEvent]) -> list[BaseException | None]:
        """
        One COPY for the whole batch: a single round trip, and far cheaper per
        row than INSERT. Row triggers still fire for every row, so the motion
        and live-fix NOTIFYs are unchanged -- they are just delivered together
        when the COPY commits.

        COPY is all-or-nothing. If Postgres rejects the data, retry row by row
        so the good rows still land and only the bad one fails. If the
        database itself is the problem, fail the batch at once: retrying each
        row would just wait out the same connection failure again and again.
        """
        if not events:
            return []
        pool = self._require_pool()
        if len(events) == 1:
            return await super().publish_many(events)
        try:
            async with pool.acquire() as conn:
                await conn.copy_records_to_table(
                    "device_locations",
                    records=[_record(event) for event in events],
                    columns=_COLUMNS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not is_row_error(exc):
                log.error("Batch of %d rows failed: %s", len(events), exc)
                return [exc] * len(events)
            log.warning(
                "Batch of %d rows rejected (%s); retrying one at a time", len(events), exc
            )
            return await super().publish_many(events)
        return [None] * len(events)

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
