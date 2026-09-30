"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass


# Log files are never kept longer than this, whatever the environment says.
MAX_LOG_RETENTION_DAYS = 30


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "off", "no")


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 5023
    # Downstream sink: "postgres" (default) or "log" (no deps, prints only).
    sink: str = "postgres"
    pg_dsn: str = "postgresql://user:pass@localhost:5432/gps"
    # Drop a connection that sends nothing for this long. Trackers heartbeat
    # every few minutes; without a timeout, dead TCP sessions accumulate.
    idle_timeout_s: float = 600.0
    # Guard against a device (or scanner) that never sends a valid start
    # marker, which would otherwise grow the read buffer without bound.
    max_buffer_bytes: int = 8192
    # The gateway is one long-lived process, so a small steady pool is right.
    # Through a transaction-mode pooler set PG_STATEMENT_CACHE_SIZE=0.
    pg_pool_min: int = 1
    pg_pool_max: int = 5
    pg_statement_cache_size: int = 100
    # Fixes are written in batches -- one COPY per batch, not one INSERT per
    # packet. A batch goes out once it holds batch_size rows, or
    # flush_interval_s after its first row, whichever comes first; under load
    # batches fill long before the interval. Devices are ACKed when their fix
    # is queued, so a fix can sit unwritten for up to flush_interval_s -- that
    # is also the most it lags on the live map, and what a crash can lose.
    # batch_size=1 turns batching off, and with it ACKs wait for the write.
    batch_size: int = 500
    flush_interval_s: float = 10.0
    # Fixes waiting to be written. When it is full, sessions stop reading
    # their sockets until the writer catches up: a stalled database slows
    # devices down over TCP rather than growing memory without bound.
    queue_max: int = 10_000
    # Log file directory; empty disables file logging (console only). Files
    # rotate daily and are deleted once older than log_retention_days, which
    # is capped at MAX_LOG_RETENTION_DAYS.
    log_dir: str = "logs"
    log_retention_days: int = 30
    # gateway-messages.log: one human-readable line per packet received
    # (see message_log.py). message_log_raw appends each packet's hex too.
    message_log: bool = True
    message_log_raw: bool = False
    # How long to wait for a tracker to answer a command (engine cut-off /
    # restore) before recording it as failed with 'no_reply'. A late answer
    # still overrides that.
    command_reply_timeout_s: float = 60.0
    # Tracker allowlist (see gps_gateway/admission.py): "enforce" refuses
    # IMEIs nobody approved, "log" admits them but records them for review,
    # "off" skips the check. The log sink has no database, so it is always off.
    allowlist: str = "enforce"

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            host=env.get("GATEWAY_HOST", cls.host),
            port=int(env.get("GATEWAY_PORT", cls.port)),
            sink=env.get("GATEWAY_SINK", cls.sink),
            pg_dsn=env.get("PG_DSN", cls.pg_dsn),
            idle_timeout_s=float(env.get("GATEWAY_IDLE_TIMEOUT", cls.idle_timeout_s)),
            max_buffer_bytes=int(env.get("GATEWAY_MAX_BUFFER", cls.max_buffer_bytes)),
            pg_pool_min=int(env.get("PG_POOL_MIN", cls.pg_pool_min)),
            pg_pool_max=int(env.get("PG_POOL_MAX", cls.pg_pool_max)),
            pg_statement_cache_size=int(
                env.get("PG_STATEMENT_CACHE_SIZE", cls.pg_statement_cache_size)
            ),
            batch_size=int(env.get("GATEWAY_BATCH_SIZE", cls.batch_size)),
            flush_interval_s=float(env.get("GATEWAY_FLUSH_INTERVAL", cls.flush_interval_s)),
            queue_max=int(env.get("GATEWAY_QUEUE_MAX", cls.queue_max)),
            log_dir=env.get("GATEWAY_LOG_DIR", cls.log_dir),
            log_retention_days=min(
                MAX_LOG_RETENTION_DAYS,
                max(1, int(env.get("GATEWAY_LOG_RETENTION_DAYS", cls.log_retention_days))),
            ),
            message_log=_flag(env.get("GATEWAY_MESSAGE_LOG"), cls.message_log),
            message_log_raw=_flag(env.get("GATEWAY_MESSAGE_LOG_RAW"), cls.message_log_raw),
            command_reply_timeout_s=float(
                env.get("GATEWAY_COMMAND_REPLY_TIMEOUT", cls.command_reply_timeout_s)
            ),
            allowlist=env.get("GATEWAY_ALLOWLIST", cls.allowlist).strip().lower(),
        )
