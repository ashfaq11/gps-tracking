"""
Account storage, behind an interface.

Separate from repository.py because it answers a different question: that one
is about where devices are, this one is about who may look. They share a
connection pool and nothing else.

The Postgres implementation is what runs in production; the in-memory one
lets the API and its tests run with no database at all.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, Sequence

from .schemas import Role, UserCreate, UserOut, UserUpdate
from .security import hash_password, token_fingerprint, verify_password

_COLUMNS = (
    "id, username, full_name, email, mobile, role, is_active, created_at, last_login_at"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PushSubscription:
    """A browser's Web Push endpoint and the two keys needed to encrypt a
    message to it (see push.py). No user_id -- callers ask "who can see this
    device", not "what did this user subscribe with"."""

    endpoint: str
    p256dh: str
    auth: str


@dataclass
class AuthenticatedUser:
    """A signed-in account, as the request handlers need it."""

    id: int
    username: str
    role: Role
    is_active: bool
    devices: list[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UserRepository(Protocol):
    async def list_users(self) -> list[UserOut]: ...

    async def get_user(self, user_id: int) -> UserOut | None: ...

    async def create_user(self, payload: UserCreate) -> UserOut: ...

    async def delete_user(self, user_id: int) -> None:
        """Hard delete -- unlike deactivation, this is only for rolling back
        a signup whose device claim failed, never a user-facing action. The
        account was live for zero requests; there is nothing to audit."""
        ...

    async def update_user(self, user_id: int, payload: UserUpdate) -> UserOut | None: ...

    async def username_taken(self, username: str) -> bool: ...

    async def email_taken(self, email: str) -> bool: ...

    async def device_owner(self, device_id: str) -> int | None: ...

    async def device_owners(self, device_ids: Sequence[str] | None) -> dict[str, str]:
        """Bulk form of device_owner, keyed to the *username* rather than the
        id -- what GET /devices actually wants to show -- so listing the
        fleet is one extra query, not one per device. `None` means "every
        claim, unfiltered" -- used to find devices claimed but never
        reported, which no device_id list handed in from LocationRepository
        would ever include."""
        ...

    async def verify_current_password(self, user_id: int, password: str) -> bool:
        """For self-service password changes (PATCH /auth/me) -- proves the
        caller still knows the account's password before changing it,
        distinct from `start_session`'s username+password check because the
        caller here is already authenticated by their token."""
        ...

    async def claim_device(self, user_id: int, device_id: str) -> bool:
        """Take ownership if the device is unowned. False means someone else
        already owns it -- decided atomically, so two simultaneous claims
        cannot both succeed."""
        ...

    async def release_device(self, device_id: str) -> bool: ...

    async def count_active_admins(self, excluding: int | None = None) -> int: ...

    async def start_session(
        self, username: str, password: str, ttl: timedelta
    ) -> tuple[str, datetime, UserOut] | None: ...

    async def resolve_token(self, token: str) -> AuthenticatedUser | None: ...

    async def end_session(self, token: str) -> None: ...

    async def ensure_bootstrap_admin(self, username: str, password: str) -> bool: ...

    async def add_subscription(
        self, user_id: int, endpoint: str, p256dh: str, auth: str
    ) -> None: ...

    async def remove_subscription(self, endpoint: str) -> None: ...

    async def subscriptions_for_device(self, device_id: str) -> list[PushSubscription]: ...


class InMemoryUserRepository:
    """Development and test backend. Not durable, not shared across workers."""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._devices: dict[int, list[str]] = {}
        self._sessions: dict[str, dict] = {}
        # device_id -> owning user id, mirroring the device_claims table.
        self._claims: dict[str, int] = {}
        # Keyed by endpoint, like the Postgres table's unique constraint.
        self._subscriptions: dict[str, dict] = {}
        self._next_id = 1

    # --- reads ---

    def _to_out(self, row: dict) -> UserOut:
        return UserOut(
            id=row["id"],
            username=row["username"],
            full_name=row["full_name"],
            email=row.get("email"),
            mobile=row.get("mobile"),
            role=row["role"],
            is_active=row["is_active"],
            devices=list(self._devices.get(row["id"], [])),
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    async def list_users(self) -> list[UserOut]:
        rows = sorted(self._rows.values(), key=lambda r: r["username"])
        return [self._to_out(row) for row in rows]

    async def get_user(self, user_id: int) -> UserOut | None:
        row = self._rows.get(user_id)
        return self._to_out(row) if row else None

    async def username_taken(self, username: str) -> bool:
        return any(row["username"] == username.lower() for row in self._rows.values())

    def _auto_claim_unowned(self, user_id: int, devices: list[str]) -> None:
        """
        Admin onboarding a device via the assignment checklist also makes
        the account its owner, provided nobody already owns it -- the admin
        equivalent of self-service signup's claim step, and what stops the
        device being claimable by a stranger later. A device someone else
        already owns is left alone: checking it here is sharing an owned
        vehicle's visibility (e.g. a dispatcher who should also see it), not
        taking it over -- release + claim is the deliberate way to do that.
        """
        for device_id in devices:
            if device_id not in self._claims:
                self._claims[device_id] = user_id

    async def delete_user(self, user_id: int) -> None:
        self._rows.pop(user_id, None)
        self._devices.pop(user_id, None)

    async def email_taken(self, email: str) -> bool:
        wanted = email.strip().lower()
        return any((row.get("email") or "").lower() == wanted for row in self._rows.values())

    async def device_owner(self, device_id: str) -> int | None:
        return self._claims.get(device_id)

    async def device_owners(self, device_ids: Sequence[str] | None) -> dict[str, str]:
        wanted = None if device_ids is None else set(device_ids)
        result: dict[str, str] = {}
        for device_id, user_id in self._claims.items():
            if (wanted is None or device_id in wanted) and user_id in self._rows:
                result[device_id] = self._rows[user_id]["username"]
        return result

    async def verify_current_password(self, user_id: int, password: str) -> bool:
        row = self._rows.get(user_id)
        return row is not None and verify_password(password, row["password_hash"])

    async def claim_device(self, user_id: int, device_id: str) -> bool:
        # Single-threaded and awaitless, so this is as atomic as the
        # Postgres primary key it stands in for.
        if device_id in self._claims:
            return False
        self._claims[device_id] = user_id
        devices = self._devices.setdefault(user_id, [])
        if device_id not in devices:
            devices.append(device_id)
        return True

    async def release_device(self, device_id: str) -> bool:
        owner = self._claims.pop(device_id, None)
        if owner is None:
            return False
        devices = self._devices.get(owner, [])
        if device_id in devices:
            devices.remove(device_id)
        return True

    async def count_active_admins(self, excluding: int | None = None) -> int:
        return sum(
            1
            for row in self._rows.values()
            if row["role"] == "admin" and row["is_active"] and row["id"] != excluding
        )

    # --- writes ---

    async def create_user(self, payload: UserCreate) -> UserOut:
        row = {
            "id": self._next_id,
            "username": payload.username.lower(),
            "password_hash": hash_password(payload.password),
            "full_name": payload.full_name,
            "email": payload.email,
            "mobile": payload.mobile,
            "role": payload.role,
            "is_active": True,
            "created_at": _now(),
            "last_login_at": None,
        }
        self._rows[row["id"]] = row
        # An admin sees everything, so per-device rows would be noise.
        devices = [] if payload.role == "admin" else list(payload.devices)
        self._devices[row["id"]] = devices
        self._auto_claim_unowned(row["id"], devices)
        self._next_id += 1
        return self._to_out(row)

    async def update_user(self, user_id: int, payload: UserUpdate) -> UserOut | None:
        row = self._rows.get(user_id)
        if row is None:
            return None

        if payload.full_name is not None:
            row["full_name"] = payload.full_name
        if payload.email is not None:
            row["email"] = payload.email
        if payload.mobile is not None:
            row["mobile"] = payload.mobile
        if payload.role is not None:
            row["role"] = payload.role
            if payload.role == "admin":
                self._devices[user_id] = []
        if payload.is_active is not None:
            row["is_active"] = payload.is_active
            if not payload.is_active:
                self._drop_sessions(user_id)
        if payload.devices is not None and row["role"] != "admin":
            self._devices[user_id] = list(payload.devices)
            self._auto_claim_unowned(user_id, payload.devices)
        if payload.password is not None:
            row["password_hash"] = hash_password(payload.password)
            # A new password invalidates whatever was signed in with the old.
            self._drop_sessions(user_id)

        return self._to_out(row)

    def _drop_sessions(self, user_id: int) -> None:
        for key in [k for k, v in self._sessions.items() if v["user_id"] == user_id]:
            del self._sessions[key]

    # --- sessions ---

    async def start_session(
        self, username: str, password: str, ttl: timedelta
    ) -> tuple[str, datetime, UserOut] | None:
        from .security import new_token, verify_password

        row = next(
            (r for r in self._rows.values() if r["username"] == username.strip().lower()), None
        )
        if row is None or not row["is_active"]:
            return None
        if not verify_password(password, row["password_hash"]):
            return None

        token = new_token()
        expires_at = _now() + ttl
        self._sessions[token_fingerprint(token)] = {"user_id": row["id"], "expires_at": expires_at}
        row["last_login_at"] = _now()
        return token, expires_at, self._to_out(row)

    async def resolve_token(self, token: str) -> AuthenticatedUser | None:
        session = self._sessions.get(token_fingerprint(token))
        if session is None or session["expires_at"] <= _now():
            return None
        row = self._rows.get(session["user_id"])
        # Deactivation takes effect here, on the very next request.
        if row is None or not row["is_active"]:
            return None
        return AuthenticatedUser(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            is_active=row["is_active"],
            devices=list(self._devices.get(row["id"], [])),
        )

    async def end_session(self, token: str) -> None:
        self._sessions.pop(token_fingerprint(token), None)

    async def ensure_bootstrap_admin(self, username: str, password: str) -> bool:
        if self._rows:
            return False
        await self.create_user(
            UserCreate(username=username, password=password, role="admin", full_name="Bootstrap")
        )
        return True

    # --- push subscriptions ---

    async def add_subscription(self, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
        self._subscriptions[endpoint] = {"user_id": user_id, "p256dh": p256dh, "auth": auth}

    async def remove_subscription(self, endpoint: str) -> None:
        self._subscriptions.pop(endpoint, None)

    async def subscriptions_for_device(self, device_id: str) -> list[PushSubscription]:
        out = []
        for endpoint, sub in self._subscriptions.items():
            row = self._rows.get(sub["user_id"])
            if row is None or not row["is_active"]:
                continue
            visible = row["role"] == "admin" or device_id in self._devices.get(row["id"], [])
            if visible:
                out.append(PushSubscription(endpoint=endpoint, p256dh=sub["p256dh"], auth=sub["auth"]))
        return out


class PostgresUserRepository:
    def __init__(self, pool):
        self._pool = pool

    async def _devices_for(self, conn, user_id: int) -> list[str]:
        rows = await conn.fetch(
            "SELECT device_id FROM user_devices WHERE user_id = $1 ORDER BY device_id", user_id
        )
        return [r["device_id"] for r in rows]

    async def _out(self, conn, row) -> UserOut:
        return UserOut(**dict(row), devices=await self._devices_for(conn, row["id"]))

    async def list_users(self) -> list[UserOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT {_COLUMNS} FROM users ORDER BY username")
            return [await self._out(conn, row) for row in rows]

    async def get_user(self, user_id: int) -> UserOut | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM users WHERE id = $1", user_id)
            return await self._out(conn, row) if row else None

    async def username_taken(self, username: str) -> bool:
        async with self._pool.acquire() as conn:
            found = await conn.fetchval("SELECT 1 FROM users WHERE username = $1", username.lower())
        return found is not None

    async def email_taken(self, email: str) -> bool:
        async with self._pool.acquire() as conn:
            found = await conn.fetchval(
                "SELECT 1 FROM users WHERE lower(email) = lower($1)", email.strip()
            )
        return found is not None

    async def delete_user(self, user_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)

    async def device_owner(self, device_id: str) -> int | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT user_id FROM device_claims WHERE device_id = $1", device_id
            )

    async def device_owners(self, device_ids: Sequence[str] | None) -> dict[str, str]:
        if device_ids is not None and not device_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT dc.device_id, u.username
                FROM device_claims dc
                JOIN users u ON u.id = dc.user_id
                WHERE $1::text[] IS NULL OR dc.device_id = ANY($1::text[])
                """,
                list(device_ids) if device_ids is not None else None,
            )
        return {r["device_id"]: r["username"] for r in rows}

    async def verify_current_password(self, user_id: int, password: str) -> bool:
        async with self._pool.acquire() as conn:
            password_hash = await conn.fetchval(
                "SELECT password_hash FROM users WHERE id = $1", user_id
            )
        return password_hash is not None and verify_password(password, password_hash)

    async def claim_device(self, user_id: int, device_id: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # ON CONFLICT DO NOTHING + RETURNING is the whole race fix:
                # the loser of a simultaneous claim gets no row back rather
                # than a second copy of the ownership.
                won = await conn.fetchval(
                    """
                    INSERT INTO device_claims (device_id, user_id) VALUES ($1, $2)
                    ON CONFLICT (device_id) DO NOTHING
                    RETURNING user_id
                    """,
                    device_id,
                    user_id,
                )
                if won is None:
                    return False
                # Ownership implies visibility; an admin can add more viewers.
                await conn.execute(
                    """
                    INSERT INTO user_devices (user_id, device_id) VALUES ($1, $2)
                    ON CONFLICT (user_id, device_id) DO NOTHING
                    """,
                    user_id,
                    device_id,
                )
                return True

    async def release_device(self, device_id: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                owner = await conn.fetchval(
                    "DELETE FROM device_claims WHERE device_id = $1 RETURNING user_id", device_id
                )
                if owner is None:
                    return False
                await conn.execute(
                    "DELETE FROM user_devices WHERE user_id = $1 AND device_id = $2",
                    owner,
                    device_id,
                )
                return True

    async def count_active_admins(self, excluding: int | None = None) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT count(*) FROM users
                WHERE role = 'admin' AND is_active
                  AND ($1::bigint IS NULL OR id <> $1)
                """,
                excluding,
            )

    async def create_user(self, payload: UserCreate) -> UserOut:
        devices = [] if payload.role == "admin" else payload.devices
        async with self._pool.acquire() as conn:
            # One transaction: an account that exists without its device
            # assignments would silently show the wrong fleet.
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO users (username, password_hash, full_name, email, mobile, role)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING {_COLUMNS}
                    """,
                    payload.username.lower(),
                    hash_password(payload.password),
                    payload.full_name,
                    payload.email,
                    payload.mobile,
                    payload.role,
                )
                await self._replace_devices(conn, row["id"], devices)
            return await self._out(conn, row)

    async def update_user(self, user_id: int, payload: UserUpdate) -> UserOut | None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow("SELECT role FROM users WHERE id = $1", user_id)
                if current is None:
                    return None

                role = payload.role or current["role"]
                row = await conn.fetchrow(
                    f"""
                    UPDATE users SET
                        full_name     = COALESCE($2, full_name),
                        role          = COALESCE($3, role),
                        is_active     = COALESCE($4, is_active),
                        password_hash = COALESCE($5, password_hash),
                        email         = COALESCE($6, email),
                        mobile        = COALESCE($7, mobile),
                        updated_at    = now()
                    WHERE id = $1
                    RETURNING {_COLUMNS}
                    """,
                    user_id,
                    payload.full_name,
                    payload.role,
                    payload.is_active,
                    hash_password(payload.password) if payload.password else None,
                    payload.email,
                    payload.mobile,
                )

                if role == "admin":
                    await conn.execute("DELETE FROM user_devices WHERE user_id = $1", user_id)
                elif payload.devices is not None:
                    await self._replace_devices(conn, user_id, payload.devices)

                # Deactivating or changing a password ends the sessions that
                # were opened under the old state.
                if payload.is_active is False or payload.password is not None:
                    await conn.execute("DELETE FROM user_sessions WHERE user_id = $1", user_id)

            return await self._out(conn, row)

    async def _replace_devices(self, conn, user_id: int, devices) -> None:
        await conn.execute("DELETE FROM user_devices WHERE user_id = $1", user_id)
        unique = sorted({d.strip() for d in devices if d and d.strip()})
        for device_id in unique:
            await conn.execute(
                "INSERT INTO user_devices (user_id, device_id) VALUES ($1, $2)", user_id, device_id
            )
            # Admin onboarding a device via the assignment checklist also
            # makes the account its owner, provided nobody already owns it --
            # the admin equivalent of self-service signup's claim step, and
            # what stops the device being claimable by a stranger later.
            # ON CONFLICT DO NOTHING is the whole rule: a device someone else
            # already owns is left alone, since ticking the box here is
            # sharing visibility into an owned vehicle, not taking it over --
            # release + claim is the deliberate way to do that.
            await conn.execute(
                """
                INSERT INTO device_claims (device_id, user_id) VALUES ($1, $2)
                ON CONFLICT (device_id) DO NOTHING
                """,
                device_id,
                user_id,
            )

    async def start_session(
        self, username: str, password: str, ttl: timedelta
    ) -> tuple[str, datetime, UserOut] | None:
        from .security import new_token, verify_password

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS}, password_hash FROM users WHERE username = $1",
                username.strip().lower(),
            )
            if row is None or not row["is_active"]:
                return None
            if not verify_password(password, row["password_hash"]):
                return None

            token = new_token()
            expires_at = _now() + ttl
            async with conn.transaction():
                # Cheap opportunistic sweep, so expired rows do not accumulate
                # without needing a scheduled job.
                await conn.execute("DELETE FROM user_sessions WHERE expires_at <= now()")
                await conn.execute(
                    "INSERT INTO user_sessions (token_hash, user_id, expires_at) "
                    "VALUES ($1, $2, $3)",
                    token_fingerprint(token),
                    row["id"],
                    expires_at,
                )
                updated = await conn.fetchrow(
                    f"UPDATE users SET last_login_at = now() WHERE id = $1 RETURNING {_COLUMNS}",
                    row["id"],
                )
            return token, expires_at, await self._out(conn, updated)

    async def resolve_token(self, token: str) -> AuthenticatedUser | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.id, u.username, u.role, u.is_active
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = $1 AND s.expires_at > now()
                """,
                token_fingerprint(token),
            )
            # The join checks is_active on every request, so deactivating an
            # account takes effect immediately rather than when its token
            # would have expired.
            if row is None or not row["is_active"]:
                return None
            return AuthenticatedUser(
                id=row["id"],
                username=row["username"],
                role=row["role"],
                is_active=row["is_active"],
                devices=await self._devices_for(conn, row["id"]),
            )

    async def end_session(self, token: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_sessions WHERE token_hash = $1", token_fingerprint(token)
            )

    async def ensure_bootstrap_admin(self, username: str, password: str) -> bool:
        """
        Create the first admin, once, so a fresh database is reachable.

        Does nothing as soon as any account exists -- otherwise the env vars
        would resurrect a deleted admin or silently reset its password.
        """
        async with self._pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM users LIMIT 1"):
                return False
        await self.create_user(
            UserCreate(username=username, password=password, role="admin", full_name="Bootstrap")
        )
        return True

    # --- push subscriptions ---

    async def add_subscription(self, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
        async with self._pool.acquire() as conn:
            # A browser that already subscribed (e.g. re-enabling the switch
            # after turning it off client-side only) re-sends the same
            # endpoint; upsert rather than error.
            await conn.execute(
                """
                INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (endpoint)
                DO UPDATE SET user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh,
                              auth = EXCLUDED.auth
                """,
                user_id,
                endpoint,
                p256dh,
                auth,
            )

    async def remove_subscription(self, endpoint: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM push_subscriptions WHERE endpoint = $1", endpoint)

    async def subscriptions_for_device(self, device_id: str) -> list[PushSubscription]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ps.endpoint, ps.p256dh, ps.auth
                FROM push_subscriptions ps
                JOIN users u ON u.id = ps.user_id
                WHERE u.is_active AND (
                    u.role = 'admin'
                    OR EXISTS (
                        SELECT 1 FROM user_devices ud
                        WHERE ud.user_id = u.id AND ud.device_id = $1
                    )
                )
                """,
                device_id,
            )
        return [PushSubscription(**dict(row)) for row in rows]
