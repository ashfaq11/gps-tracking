"""API settings, read from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 55920
    pg_dsn: str = "postgresql://user:pass@localhost:5432/gps"
    # Repository backend: "postgres", or "memory" for local development
    # without a database.
    backend: str = "postgres"
    # Shared secret required by the HTTP ingest endpoint. Ingest is disabled
    # entirely when this is unset, so it can never be left open by accident.
    ingest_api_key: str | None = None
    max_page_size: int = 1000
    # How long a dashboard sign-in lasts.
    session_ttl_hours: int = 12
    # Seeds the first admin when the users table is empty, so a fresh
    # database is reachable at all. Ignored once any account exists.
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: str | None = None
    # Web Push (RFC 8292). Generate a pair with `python -m api.push genkey`.
    # Push is disabled -- the subscribe endpoint answers 503 -- whenever
    # either half is unset, the same "off unless configured" shape as
    # ingest_api_key.
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    # A contact URL/mailto a push service can reach if it needs to reach the
    # sender -- required by the VAPID spec, not a secret.
    vapid_subject: str = "mailto:admin@example.com"
    # Requests at or above this are logged as warnings.
    slow_request_ms: float = 1000.0
    # Connection pool sizing. Serverless platforms run many short-lived
    # instances, each with its own pool, so a generous pool per instance
    # exhausts the database's connection limit. On Vercel use 0/1 against a
    # transaction-mode pooler.
    pg_pool_min: int = 1
    pg_pool_max: int = 10
    # asyncpg caches prepared statements, which a transaction-mode pooler
    # (PgBouncer, Supabase Supavisor on port 6543) cannot support because a
    # connection is handed to a different client between statements. Set 0
    # when connecting through one.
    pg_statement_cache_size: int = 100
    cors_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env=None) -> "ApiConfig":
        env = os.environ if env is None else env
        origins = env.get("API_CORS_ORIGINS", "")
        return cls(
            host=env.get("API_HOST", cls.host),
            port=int(env.get("API_PORT", cls.port)),
            pg_dsn=env.get("PG_DSN", cls.pg_dsn),
            backend=env.get("API_BACKEND", cls.backend),
            ingest_api_key=env.get("INGEST_API_KEY") or None,
            max_page_size=int(env.get("API_MAX_PAGE_SIZE", cls.max_page_size)),
            session_ttl_hours=int(env.get("SESSION_TTL_HOURS", cls.session_ttl_hours)),
            bootstrap_admin_username=env.get("BOOTSTRAP_ADMIN_USERNAME") or None,
            bootstrap_admin_password=env.get("BOOTSTRAP_ADMIN_PASSWORD") or None,
            vapid_public_key=env.get("VAPID_PUBLIC_KEY") or None,
            vapid_private_key=env.get("VAPID_PRIVATE_KEY") or None,
            vapid_subject=env.get("VAPID_SUBJECT", cls.vapid_subject),
            slow_request_ms=float(env.get("API_SLOW_REQUEST_MS", cls.slow_request_ms)),
            pg_pool_min=int(env.get("PG_POOL_MIN", cls.pg_pool_min)),
            pg_pool_max=int(env.get("PG_POOL_MAX", cls.pg_pool_max)),
            pg_statement_cache_size=int(
                env.get("PG_STATEMENT_CACHE_SIZE", cls.pg_statement_cache_size)
            ),
            cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
        )
