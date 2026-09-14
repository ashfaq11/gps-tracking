-- GPS ingestion gateway schema.
-- Run once before starting the gateway:
--     psql "$PG_DSN" -f sql/schema.sql
-- Safe to re-run: every statement is idempotent.

CREATE TABLE IF NOT EXISTS device_locations (
    id          BIGSERIAL PRIMARY KEY,
    device_id   TEXT             NOT NULL,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    speed_kmh   INT,
    course_deg  INT,
    gps_fixed   BOOLEAN,
    satellites  SMALLINT,
    -- When the device says the fix was taken. NULL if its clock was unset.
    fixed_at    TIMESTAMPTZ,
    -- When the gateway decoded the packet.
    received_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Added separately so the script also upgrades a table created by an
-- older version of the gateway, which lacked these columns.
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS satellites SMALLINT;
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS fixed_at TIMESTAMPTZ;

-- The dashboard query is "latest fixes for this device", newest first.
CREATE INDEX IF NOT EXISTS device_locations_device_time_idx
    ON device_locations (device_id, received_at DESC);


-- ---------------------------------------------------------------------------
-- Dashboard accounts.
--
-- Only the web dashboard uses these. The gateway never reads them: a tracker
-- authenticates by IMEI over TCP and has no idea who owns it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    -- Stored lower-cased, so sign-in can be case-insensitive without a
    -- functional index and two accounts cannot differ only by case.
    username      TEXT        NOT NULL UNIQUE,
    -- bcrypt output, salt included. Never a plaintext password.
    password_hash TEXT        NOT NULL,
    -- 'admin' sees every device and manages accounts; 'user' sees only the
    -- devices assigned in user_devices.
    role          TEXT        NOT NULL DEFAULT 'user',
    -- Deactivation is a flag, not a delete: an account is part of the audit
    -- trail, and reactivating is one click rather than a restore.
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    full_name     TEXT,
    -- Contact details, collected at signup. Unique email because it is the
    -- account's support and recovery handle; neither is verified yet (no
    -- mail or SMS provider is wired up), so treat both as self-declared.
    email         TEXT UNIQUE,
    mobile        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    CONSTRAINT users_role_known CHECK (role IN ('admin', 'user'))
);

-- Added separately so the script also upgrades a table created before these
-- columns existed.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email  TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mobile TEXT;
DO $users_email_unique$
BEGIN
    ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE (email);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END
$users_email_unique$;


-- Which devices a non-admin account may see; an admin needs no rows here.
-- device_id is deliberately not a foreign key: devices are never registered
-- -- one exists the moment it reports -- so an admin must be able to assign
-- a device before its first fix arrives, and so a signup can claim one.
CREATE TABLE IF NOT EXISTS user_devices (
    user_id     BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    device_id   TEXT        NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, device_id)
);

-- "Who owns this device?" -- the question every signup and device claim asks,
-- and the one the primary key above cannot answer: its leading column is
-- user_id, so a lookup by device_id alone would scan the table.
CREATE INDEX IF NOT EXISTS user_devices_device_idx ON user_devices (device_id);

-- ---------------------------------------------------------------------------
-- Device ownership.
--
-- Separate from user_devices, which is *visibility* and stays many-to-many:
-- an admin can grant several dispatchers sight of the same vehicle. This is
-- *ownership*, and there is exactly one owner, so device_id is the primary
-- key -- which is also what makes claiming race-safe. Two people signing up
-- with the same IMEI at the same instant both run an INSERT; the key lets
-- exactly one win, with no check-then-act window between them.
--
-- There is no activation code: first claim wins. The API narrows that in
-- two ways (see routers/users.py's signup) -- a device must already have
-- reported a position to be claimable, so an IMEI range cannot be claimed
-- before the hardware ships, and a claimed device stays claimed until an
-- admin releases it for a new owner.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_claims (
    device_id  TEXT        PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "What does this account own?" -- the reverse of the primary key's question.
CREATE INDEX IF NOT EXISTS device_claims_user_idx ON device_claims (user_id);


-- Sign-in tokens.
--
-- Opaque and stored, rather than self-contained, so that deactivating an
-- account takes effect on its next request. A stateless token would stay
-- valid until it expired, which is exactly the wrong behaviour for a
-- "deactivate this user" button.
--
-- Only the hash is kept, so a leaked table does not hand over live sessions.
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT        PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS user_sessions_user_idx ON user_sessions (user_id);
-- Expired rows are swept on sign-in; this keeps that cheap.
CREATE INDEX IF NOT EXISTS user_sessions_expiry_idx ON user_sessions (expires_at);


-- ---------------------------------------------------------------------------
-- Device subscriptions.
--
-- Billing is per tracked device, not per dashboard account -- one row per
-- device that has had any of these dates set. No row, or a null
-- subscription_end_date within one, means "unmetered": every device is
-- fully visible until an admin opts it into metering at all, so turning the
-- feature on cannot silently hide an existing fleet.
--
-- installed_at and sim_expiry_date ride along on the same row since they are
-- the same kind of fact about the same device, but they are informational
-- only -- unlike subscription_end_date, neither one hides anything. The
-- software has no way to enforce a SIM staying connected; sim_expiry_date is
-- a heads-up for a human, not a gate.
--
-- device_id is deliberately not a foreign key, for the same reason
-- user_devices.device_id is not: devices are never registered, so an admin
-- must be able to set any of these before the device's first fix arrives.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_subscriptions (
    device_id             TEXT        PRIMARY KEY,
    subscription_end_date TIMESTAMPTZ,
    -- When the tracker was fitted to the vehicle and put into service.
    installed_at          TIMESTAMPTZ,
    -- When the tracker's cellular SIM / data plan runs out.
    sim_expiry_date       TIMESTAMPTZ,
    -- Cosmetic, unlike everything else in this table: an operator-set label.
    -- Never hides a device or its data -- see api/routers/devices.py's
    -- profile endpoint, which any account that can see the device may call,
    -- not just an admin. The marker shape is a separate table, below.
    name                  TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Added separately so the script also upgrades a table created before these
-- columns existed, and relaxes subscription_end_date for a table created
-- back when a row could not exist without one.
ALTER TABLE device_subscriptions ALTER COLUMN subscription_end_date DROP NOT NULL;
ALTER TABLE device_subscriptions ADD COLUMN IF NOT EXISTS installed_at TIMESTAMPTZ;
ALTER TABLE device_subscriptions ADD COLUMN IF NOT EXISTS sim_expiry_date TIMESTAMPTZ;
ALTER TABLE device_subscriptions ADD COLUMN IF NOT EXISTS name TEXT;


-- ---------------------------------------------------------------------------
-- Device markers: which vehicle shape each device draws on the map.
--
-- A dedicated mapping table, not a column on device_subscriptions -- that
-- table is billing state; this is a separate, purely cosmetic concern, and
-- keeping it apart means neither can get tangled in the other's rules as
-- either grows. `marker_code` is the single source of truth the UI maps to
-- an image/SVG (see gps-tracking-web's shared/map/vehicle-icons.ts); add a
-- vehicle shape here and to the CHECK below together with adding it there.
-- No row for a device_id means the default -- a car, the common case for a
-- fleet -- same reasoning as an unmetered device needing no
-- device_subscriptions row either.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_markers (
    device_id   TEXT        PRIMARY KEY,
    marker_code TEXT        NOT NULL DEFAULT 'car'
        CHECK (marker_code IN ('car', 'bike', 'auto', 'van', 'truck', 'bus')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One-time migration for an install that ran the earlier version of this
-- schema, which kept the marker choice as device_subscriptions.icon.
DO $migrate_markers$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'device_subscriptions' AND column_name = 'icon'
    ) THEN
        INSERT INTO device_markers (device_id, marker_code)
        SELECT device_id, icon FROM device_subscriptions
        ON CONFLICT (device_id) DO NOTHING;

        ALTER TABLE device_subscriptions DROP COLUMN icon;
    END IF;
END
$migrate_markers$;

-- One row per change to a device's subscription_end_date -- who moved it,
-- and from/to what. Mirrors user_subscription_history's reasoning, but keyed
-- by device_id since the subscription belongs to the device, not an account.
CREATE TABLE IF NOT EXISTS device_subscription_history (
    id                BIGSERIAL   PRIMARY KEY,
    device_id         TEXT        NOT NULL,
    previous_end_date TIMESTAMPTZ,
    -- Null here means the device was cleared back to unmetered, not that a
    -- date was omitted -- every row is a real change, one way or the other.
    new_end_date      TIMESTAMPTZ,
    -- Nullable and ON DELETE SET NULL, not CASCADE: the history should
    -- survive even if the admin who made the change is later removed.
    changed_by        BIGINT      REFERENCES users (id) ON DELETE SET NULL,
    changed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS device_subscription_history_device_idx
    ON device_subscription_history (device_id, changed_at DESC);


-- ---------------------------------------------------------------------------
-- Web Push subscriptions.
--
-- One row per browser a user has turned notifications on in. `endpoint` is
-- the push service URL the browser handed back from
-- `PushManager.subscribe()` (unique per browser install) and is itself the
-- natural key -- a user with three browsers has three rows.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    endpoint   TEXT        NOT NULL UNIQUE,
    -- The subscription's public key and auth secret, both required to
    -- encrypt a push message per the Web Push spec (RFC 8291).
    p256dh     TEXT        NOT NULL,
    auth       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS push_subscriptions_user_idx ON push_subscriptions (user_id);


-- ---------------------------------------------------------------------------
-- Vehicle start/stop notifications.
--
-- Two independent processes write to device_locations -- the REST ingest
-- endpoint and the TCP gateway -- so detecting "this device just started or
-- stopped moving" in application code would mean doing it twice and hoping
-- both copies stay in sync. A trigger sees every insert from both writers
-- and cannot drift, which is the same reasoning as the devices registry's
-- last_seen/fix_count columns.
--
-- The trigger only publishes a NOTIFY; it does not know how to reach a
-- browser, and must not block an insert on that. A listener in the API
-- process (see api/push.py) picks the notification up and sends the actual
-- push messages outside the database transaction entirely.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION notify_vehicle_motion() RETURNS trigger AS $$
DECLARE
    previous_speed INT;
    was_moving     BOOLEAN;
    is_moving      BOOLEAN;
    last_moving_at TIMESTAMPTZ;
BEGIN
    -- "Previous" means strictly earlier, not merely "any other row". The
    -- gateway writes fixes in multi-row batches, and row triggers fire at the
    -- end of the statement -- by then the later rows of the same batch are
    -- already visible, and `id <> NEW.id` would compare a fix against the one
    -- after it, firing a bogus stopped/started notification. The plain
    -- `received_at <=` is implied by the row comparison, but spelled out so
    -- the (device_id, received_at) index can seek to it rather than scan
    -- past newer rows -- this runs once for every fix stored.
    SELECT speed_kmh INTO previous_speed
    FROM device_locations
    WHERE device_id = NEW.device_id
      AND received_at <= NEW.received_at
      AND (received_at, id) < (NEW.received_at, NEW.id)
    ORDER BY received_at DESC, id DESC
    LIMIT 1;

    -- Nothing to compare against yet -- this is the device's first-ever fix.
    IF previous_speed IS NULL THEN
        RETURN NEW;
    END IF;

    was_moving := previous_speed > 0;
    is_moving  := COALESCE(NEW.speed_kmh, 0) > 0;

    IF was_moving = is_moving THEN
        RETURN NEW;
    END IF;

    -- A "started moving" alert is only useful for a real halt -- a stop at
    -- a traffic light and an actual rest break both read as "stopped" the
    -- instant they happen, and only the gap before it moves again tells
    -- them apart. Require the vehicle to have been stopped at least ten
    -- minutes before alerting that it moved again; "stopped" itself still
    -- fires immediately, since there is nothing to wait for at that end.
    IF is_moving THEN
        SELECT received_at INTO last_moving_at
        FROM device_locations
        WHERE device_id = NEW.device_id
          AND received_at <= NEW.received_at
          AND (received_at, id) < (NEW.received_at, NEW.id)
          AND speed_kmh > 0
        ORDER BY received_at DESC, id DESC
        LIMIT 1;

        -- last_moving_at NULL means no prior moving fix at all -- it has
        -- been stopped since it was first seen, which certainly clears ten
        -- minutes, so only the non-null case can fall short of it.
        -- (INTERVAL has no representation for "infinity" on this Postgres
        -- version, so this skips the subtraction entirely rather than
        -- computing against it.)
        IF last_moving_at IS NOT NULL AND NEW.received_at - last_moving_at < INTERVAL '10 minutes' THEN
            RETURN NEW;
        END IF;
    END IF;

    PERFORM pg_notify('vehicle_motion', json_build_object(
        'device_id', NEW.device_id,
        'moving', is_moving,
        'speed_kmh', NEW.speed_kmh,
        'received_at', NEW.received_at
    )::text);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS device_locations_notify_motion ON device_locations;
CREATE TRIGGER device_locations_notify_motion
    AFTER INSERT ON device_locations
    FOR EACH ROW EXECUTE FUNCTION notify_vehicle_motion();


-- ---------------------------------------------------------------------------
-- Live location.
--
-- notify_vehicle_motion above only fires on a start/stop transition -- fine
-- for an occasional alert, wrong for a map that should move smoothly. This
-- fires on every single fix, from either writer, so a connected dashboard
-- can show a new position the instant it lands instead of waiting for its
-- next poll. See api/live.py for the listener that turns this into a
-- WebSocket message.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION notify_new_fix() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('device_fix', json_build_object(
        'id', NEW.id,
        'device_id', NEW.device_id,
        'latitude', NEW.latitude,
        'longitude', NEW.longitude,
        'speed_kmh', NEW.speed_kmh,
        'course_deg', NEW.course_deg,
        'gps_fixed', NEW.gps_fixed,
        'satellites', NEW.satellites,
        'fixed_at', NEW.fixed_at,
        'received_at', NEW.received_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS device_locations_notify_fix ON device_locations;
CREATE TRIGGER device_locations_notify_fix
    AFTER INSERT ON device_locations
    FOR EACH ROW EXECUTE FUNCTION notify_new_fix();
