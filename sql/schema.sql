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

-- Serving cell tower (LBS) the tracker reported alongside the fix: mobile
-- country code, mobile network code, location/tracking area code, cell id.
-- NULL together when the packet carried none. Kept on the fix itself, not in
-- a side table, so both writers fill them the same way; they are what a
-- geolocation lookup needs to place a device whose GPS fix is stale.
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS mcc     SMALLINT;
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS mnc     SMALLINT;
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS lac     INT;
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS cell_id BIGINT;

-- Ignition (ACC) as reported by the packet that produced this row -- GT06
-- alarms carry it, plain location packets do not, HTTP clients may send it.
-- NULL means "not reported", never "off". The device's *current* ignition,
-- which mostly arrives in heartbeats with no position at all, is
-- device_status below.
ALTER TABLE device_locations ADD COLUMN IF NOT EXISTS ignition BOOLEAN;

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
ALTER TABLE device_subscriptions ADD COLUMN IF NOT EXISTS secret_code TEXT UNIQUE;

CREATE INDEX IF NOT EXISTS device_subscriptions_secret_code_idx
    ON device_subscriptions (secret_code) WHERE secret_code IS NOT NULL;


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
        'received_at', NEW.received_at,
        'mcc', NEW.mcc,
        'mnc', NEW.mnc,
        'lac', NEW.lac,
        'cell_id', NEW.cell_id,
        'ignition', NEW.ignition
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS device_locations_notify_fix ON device_locations;
CREATE TRIGGER device_locations_notify_fix
    AFTER INSERT ON device_locations
    FOR EACH ROW EXECUTE FUNCTION notify_new_fix();


-- ---------------------------------------------------------------------------
-- Ignition.
--
-- A GT06 reports ignition in its heartbeat, which has no position, and a
-- parked vehicle often sends nothing *but* heartbeats -- so "ignition off"
-- cannot wait for the next device_locations row. device_status holds each
-- device's current state; record_ignition() is its only writer, called by
-- the gateway for every heartbeat and by the trigger below for any position
-- row that carries ignition (GT06 alarms, HTTP ingest). One function, so
-- both writers agree on what counts as a change.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_status (
    device_id           TEXT PRIMARY KEY,
    ignition            BOOLEAN     NOT NULL,
    -- When ignition last flipped; the first report counts as a flip.
    ignition_changed_at TIMESTAMPTZ NOT NULL,
    -- When the newest report was received, whatever it said.
    reported_at         TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION record_ignition(
    p_device_id TEXT, p_ignition BOOLEAN, p_at TIMESTAMPTZ
) RETURNS void AS $$
    INSERT INTO device_status AS s (device_id, ignition, ignition_changed_at, reported_at)
    VALUES (p_device_id, p_ignition, p_at, p_at)
    ON CONFLICT (device_id) DO UPDATE SET
        ignition = EXCLUDED.ignition,
        ignition_changed_at = CASE
            WHEN s.ignition IS DISTINCT FROM EXCLUDED.ignition THEN EXCLUDED.reported_at
            ELSE s.ignition_changed_at
        END,
        reported_at = EXCLUDED.reported_at
    -- The gateway batches position rows but writes heartbeats at once, so an
    -- alarm row can land after a newer heartbeat. Older news never wins.
    WHERE EXCLUDED.reported_at >= s.reported_at;
-- SECURITY DEFINER for the same reason as check_geofences below: every
-- writer must be able to record ignition without its own grant on the table.
$$ LANGUAGE sql SECURITY DEFINER SET search_path = public;

CREATE OR REPLACE FUNCTION record_row_ignition() RETURNS trigger AS $$
BEGIN
    PERFORM record_ignition(NEW.device_id, NEW.ignition, NEW.received_at);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS device_locations_record_ignition ON device_locations;
CREATE TRIGGER device_locations_record_ignition
    AFTER INSERT ON device_locations
    FOR EACH ROW WHEN (NEW.ignition IS NOT NULL)
    EXECUTE FUNCTION record_row_ignition();

-- Ignition history: one row per flip, the first report included.
--
-- device_status only knows *now*. How long an engine sat running with the
-- vehicle parked -- the driving report's idle time, api/driving.py -- needs
-- every switch-on and switch-off, so they are kept here. Written by a
-- trigger on device_status rather than inside record_ignition(), so that
-- function stays the single small statement it is, and anything else that
-- ever writes device_status is logged too.
--
-- The log starts when this table is created: a vehicle's earlier ignition
-- is unknown, which the report says as such rather than as "never idled".
CREATE TABLE IF NOT EXISTS device_ignition_log (
    id        BIGSERIAL PRIMARY KEY,
    device_id TEXT        NOT NULL,
    ignition  BOOLEAN     NOT NULL,
    at        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS device_ignition_log_device_at_idx
    ON device_ignition_log (device_id, at);

-- SECURITY DEFINER for the same reason as record_ignition(): whoever
-- writes a heartbeat must be able to log it without a grant of their own.
CREATE OR REPLACE FUNCTION log_ignition_change() RETURNS trigger AS $$
BEGIN
    INSERT INTO device_ignition_log (device_id, ignition, at)
    VALUES (NEW.device_id, NEW.ignition, NEW.ignition_changed_at);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

DROP TRIGGER IF EXISTS device_status_log_first ON device_status;
CREATE TRIGGER device_status_log_first
    AFTER INSERT ON device_status
    FOR EACH ROW EXECUTE FUNCTION log_ignition_change();

DROP TRIGGER IF EXISTS device_status_log_flip ON device_status;
CREATE TRIGGER device_status_log_flip
    AFTER UPDATE ON device_status
    FOR EACH ROW WHEN (OLD.ignition IS DISTINCT FROM NEW.ignition)
    EXECUTE FUNCTION log_ignition_change();

-- Vehicles already known when the log is created start from their current
-- state, so an engine that is running right now is not missed until its
-- next switch. Only for a device with no log yet, so re-running is a no-op.
INSERT INTO device_ignition_log (device_id, ignition, at)
SELECT s.device_id, s.ignition, s.ignition_changed_at
FROM device_status s
WHERE NOT EXISTS (SELECT 1 FROM device_ignition_log l WHERE l.device_id = s.device_id);

-- The API reads device_status (the device list shows ignition). Same
-- ownership problem, and same fix, as the geofence grants at the end.
DO $grant_device_status$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT DISTINCT who FROM (
            SELECT c.relowner::regrole::text AS who
            FROM pg_class c WHERE c.relname = 'device_locations'
            UNION
            SELECT a.grantee::regrole::text
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.relname = 'device_locations' AND a.privilege_type = 'INSERT' AND a.grantee <> 0
        ) AS writers
    LOOP
        EXECUTE format('GRANT SELECT ON device_status, device_ignition_log TO %s', r.who);
    END LOOP;
END
$grant_device_status$;


-- ---------------------------------------------------------------------------
-- Geofences.
--
-- An area drawn on the map -- a polygon, or a circle around a point -- and
-- the vehicles it watches. When one of those vehicles crosses its edge, the
-- crossing is recorded in geofence_events (the report) and announced with a
-- NOTIFY that api/push.py turns into a push to the geofence's owner.
--
-- Detection is a trigger for the same reason notify_vehicle_motion is: both
-- writers (the ingest endpoint and the gateway's COPY) store positions, and
-- only the database sees every one of them.
--
-- No PostGIS: the geometry is small enough to do by hand -- ray casting
-- for a polygon, haversine for a circle -- and needing an extension would
-- tie the schema to hosts that offer it. The bounding box columns let the
-- trigger skip the polygon walk for a point nowhere near it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS geofences (
    id             BIGSERIAL        PRIMARY KEY,
    -- Whoever drew it. Their push subscriptions get its alerts; deleting the
    -- account removes their geofences with it.
    owner_id       BIGINT           NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name           TEXT             NOT NULL,
    kind           TEXT             NOT NULL CHECK (kind IN ('polygon', 'circle')),
    -- Polygon: vertices in order, the last joined back to the first.
    vertex_lats    DOUBLE PRECISION[],
    vertex_lngs    DOUBLE PRECISION[],
    -- Circle.
    center_lat     DOUBLE PRECISION,
    center_lng     DOUBLE PRECISION,
    radius_m       DOUBLE PRECISION,
    min_lat        DOUBLE PRECISION NOT NULL,
    max_lat        DOUBLE PRECISION NOT NULL,
    min_lng        DOUBLE PRECISION NOT NULL,
    max_lng        DOUBLE PRECISION NOT NULL,
    alert_on_exit  BOOLEAN          NOT NULL DEFAULT TRUE,
    alert_on_enter BOOLEAN          NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'polygon' AND cardinality(vertex_lats) >= 3
            AND cardinality(vertex_lats) = cardinality(vertex_lngs))
        OR (kind = 'circle' AND center_lat IS NOT NULL AND center_lng IS NOT NULL
            AND radius_m > 0)
    )
);

CREATE INDEX IF NOT EXISTS geofences_owner_idx ON geofences (owner_id);

-- Which vehicles a geofence watches.
CREATE TABLE IF NOT EXISTS geofence_devices (
    geofence_id BIGINT NOT NULL REFERENCES geofences (id) ON DELETE CASCADE,
    device_id   TEXT   NOT NULL,
    PRIMARY KEY (geofence_id, device_id)
);

CREATE INDEX IF NOT EXISTS geofence_devices_device_idx ON geofence_devices (device_id);

-- Last known inside/outside per geofence and vehicle -- what a new position
-- is compared against. No row yet means "not known": the first position
-- after a geofence is drawn (or redrawn, or a vehicle added to it) only sets
-- this, it never alerts, so drawing a fence round a vehicle that is already
-- outside it does not immediately report an "exit" that never happened.
CREATE TABLE IF NOT EXISTS geofence_device_state (
    geofence_id BIGINT      NOT NULL REFERENCES geofences (id) ON DELETE CASCADE,
    device_id   TEXT        NOT NULL,
    inside      BOOLEAN     NOT NULL,
    -- The position this was decided from, so a late-arriving older one
    -- (a tracker uploading its backlog) cannot overwrite a newer answer.
    as_of       TIMESTAMPTZ NOT NULL,
    fix_id      BIGINT      NOT NULL,
    PRIMARY KEY (geofence_id, device_id)
);

-- The report: every crossing, both ways, whether or not it alerted.
CREATE TABLE IF NOT EXISTS geofence_events (
    id          BIGSERIAL        PRIMARY KEY,
    geofence_id BIGINT           NOT NULL REFERENCES geofences (id) ON DELETE CASCADE,
    device_id   TEXT             NOT NULL,
    kind        TEXT             NOT NULL CHECK (kind IN ('exit', 'enter')),
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    -- When the vehicle crossed (the device's own clock when it has one),
    -- not when the row was written.
    occurred_at TIMESTAMPTZ      NOT NULL,
    recorded_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS geofence_events_fence_idx ON geofence_events (geofence_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS geofence_events_device_idx ON geofence_events (device_id, occurred_at DESC);

-- Great-circle distance in metres.
CREATE OR REPLACE FUNCTION geo_distance_m(
    lat1 DOUBLE PRECISION, lng1 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION, lng2 DOUBLE PRECISION
) RETURNS DOUBLE PRECISION AS $$
    SELECT 2 * 6371000 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lng2 - lng1) / 2), 2)
    ));
$$ LANGUAGE sql IMMUTABLE STRICT;

-- Whether a point is inside a geofence. Ray casting for a polygon: count
-- how many edges a ray heading east from the point crosses; odd is inside.
-- Plain lat/lng as a flat plane is fine at geofence scale (a few km) and
-- away from the antimeridian, which no fence in India goes near.
CREATE OR REPLACE FUNCTION geofence_contains(
    g geofences, lat DOUBLE PRECISION, lng DOUBLE PRECISION
) RETURNS BOOLEAN AS $$
DECLARE
    n      INT;
    i      INT;
    j      INT;
    inside BOOLEAN := FALSE;
BEGIN
    IF g.kind = 'circle' THEN
        RETURN geo_distance_m(g.center_lat, g.center_lng, lat, lng) <= g.radius_m;
    END IF;

    IF lat < g.min_lat OR lat > g.max_lat OR lng < g.min_lng OR lng > g.max_lng THEN
        RETURN FALSE;
    END IF;

    n := cardinality(g.vertex_lats);
    j := n;
    FOR i IN 1..n LOOP
        IF (g.vertex_lats[i] > lat) <> (g.vertex_lats[j] > lat)
           AND lng < (g.vertex_lngs[j] - g.vertex_lngs[i]) * (lat - g.vertex_lats[i])
                     / (g.vertex_lats[j] - g.vertex_lats[i]) + g.vertex_lngs[i] THEN
            inside := NOT inside;
        END IF;
        j := i;
    END LOOP;
    RETURN inside;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION check_geofences() RETURNS trigger AS $$
DECLARE
    g           geofences%ROWTYPE;
    now_inside  BOOLEAN;
    prev        geofence_device_state%ROWTYPE;
    crossing    TEXT;
    event_id    BIGINT;
    crossed_at  TIMESTAMPTZ;
    device_name TEXT;
BEGIN
    -- A fix without a GPS lock repeats the last known coordinates at best;
    -- judging a boundary on it would invent crossings.
    IF NEW.gps_fixed IS FALSE THEN
        RETURN NEW;
    END IF;

    -- Same rule the app uses for "when was this": the device's own clock,
    -- unless it is plainly wrong (ahead of arrival, or a month behind).
    crossed_at := CASE
        WHEN NEW.fixed_at IS NULL
          OR NEW.fixed_at > NEW.received_at + INTERVAL '10 minutes'
          OR NEW.fixed_at < NEW.received_at - INTERVAL '30 days'
        THEN NEW.received_at
        ELSE NEW.fixed_at
    END;

    FOR g IN
        SELECT f.* FROM geofences f
        JOIN geofence_devices d ON d.geofence_id = f.id
        WHERE d.device_id = NEW.device_id
    LOOP
        now_inside := geofence_contains(g, NEW.latitude, NEW.longitude);

        SELECT * INTO prev FROM geofence_device_state
        WHERE geofence_id = g.id AND device_id = NEW.device_id
        FOR UPDATE;

        IF NOT FOUND THEN
            INSERT INTO geofence_device_state (geofence_id, device_id, inside, as_of, fix_id)
            VALUES (g.id, NEW.device_id, now_inside, NEW.received_at, NEW.id)
            ON CONFLICT (geofence_id, device_id) DO NOTHING;
            CONTINUE;
        END IF;

        -- Older than what the state was already decided from -- see the
        -- strictly-earlier note on notify_vehicle_motion.
        IF (NEW.received_at, NEW.id) < (prev.as_of, prev.fix_id) THEN
            CONTINUE;
        END IF;

        UPDATE geofence_device_state
        SET inside = now_inside, as_of = NEW.received_at, fix_id = NEW.id
        WHERE geofence_id = g.id AND device_id = NEW.device_id;

        IF prev.inside = now_inside THEN
            CONTINUE;
        END IF;

        crossing := CASE WHEN now_inside THEN 'enter' ELSE 'exit' END;
        INSERT INTO geofence_events (geofence_id, device_id, kind, latitude, longitude, occurred_at)
        VALUES (g.id, NEW.device_id, crossing, NEW.latitude, NEW.longitude, crossed_at)
        RETURNING id INTO event_id;

        IF (crossing = 'exit' AND g.alert_on_exit) OR (crossing = 'enter' AND g.alert_on_enter) THEN
            SELECT name INTO device_name FROM device_subscriptions WHERE device_id = NEW.device_id;
            PERFORM pg_notify('geofence_event', json_build_object(
                'id', event_id,
                'geofence_id', g.id,
                'geofence_name', g.name,
                'owner_id', g.owner_id,
                'device_id', NEW.device_id,
                'device_name', device_name,
                'kind', crossing,
                'occurred_at', crossed_at
            )::text);
        END IF;
    END LOOP;

    RETURN NEW;
END;
-- SECURITY DEFINER: this runs inside every position insert, from the API and
-- the gateway alike, and those may connect as database users that were never
-- given the geofence tables -- without it, one missing GRANT would make every
-- position from that writer fail to save. It runs as whoever applied this
-- file instead; the pinned search_path is what makes that safe.
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

DROP TRIGGER IF EXISTS device_locations_check_geofences ON device_locations;
CREATE TRIGGER device_locations_check_geofences
    AFTER INSERT ON device_locations
    FOR EACH ROW EXECUTE FUNCTION check_geofences();

-- The API reads and writes the geofence tables directly, and this file is
-- often applied as a superuser (`sudo -u postgres psql -f ...`), which leaves
-- the new tables owned by that superuser and unreadable to the API's own
-- user. So: whoever may already save positions (the API's and the gateway's
-- users -- device_locations' owner and anyone granted INSERT on it) gets the
-- geofence tables too. Re-running this is harmless.
DO $grant_geofences$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT DISTINCT who FROM (
            SELECT c.relowner::regrole::text AS who
            FROM pg_class c WHERE c.relname = 'device_locations'
            UNION
            SELECT a.grantee::regrole::text
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.relname = 'device_locations' AND a.privilege_type = 'INSERT' AND a.grantee <> 0
        ) AS writers
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON geofences, geofence_devices, '
            'geofence_device_state, geofence_events TO %s', r.who);
        EXECUTE format(
            'GRANT USAGE, SELECT ON SEQUENCE geofences_id_seq, geofence_events_id_seq TO %s', r.who);
    END LOOP;
END
$grant_geofences$;


-- ---------------------------------------------------------------------------
-- Device commands: engine cut-off / restore over the tracker's own connection.
--
-- The API inserts a row; the trigger below tells the gateway, which holds the
-- tracker's TCP connection, and the gateway sends the text (the PT06 manual's
-- "RELAY,1#" cut fuel, "RELAY,0#" resume) as a GT06 0x80 packet. The row is
-- also the audit log: who asked, when, what was sent, what the tracker said.
--
-- Safety lives here, not in either process, because it has to hold at the
-- moment of delivery -- a command can wait in the queue while the tracker is
-- offline, and the ignition can come on meanwhile:
-- - a cut is only ever delivered to a device an admin switched cut-off on
--   for (device_relay; off by default), otherwise it fails 'relay_disabled'
-- - a cut is only ever delivered while device_status says ignition is off
--   (unknown counts as not off); otherwise it fails with 'ignition_on'
-- - an undelivered command expires at expires_at, so a tracker coming back
--   online hours later never acts on a stale request
-- - a newer command for the same device supersedes any still queued

CREATE TABLE IF NOT EXISTS device_commands (
    id           BIGSERIAL PRIMARY KEY,
    device_id    TEXT        NOT NULL,
    action       TEXT        NOT NULL CHECK (action IN ('cut', 'restore', 'timer', 'param')),
    -- The exact text sent to the tracker.
    command      TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'queued' CHECK (status IN (
                     'queued', 'sent', 'confirmed', 'failed', 'expired', 'superseded')),
    requested_by BIGINT      REFERENCES users(id) ON DELETE SET NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    sent_at      TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    -- What the tracker answered, verbatim.
    reply        TEXT,
    -- Why it did not go through: 'ignition_on', 'no_reply', ...
    error        TEXT
);

-- Tracker settings share the queue: 'timer' (TIMER,T1,T2#, the upload
-- interval) and 'param' (PARAM#, which answers with the current settings).
-- Widened here for databases created when only cut/restore existed.
ALTER TABLE device_commands DROP CONSTRAINT IF EXISTS device_commands_action_check;
ALTER TABLE device_commands ADD CONSTRAINT device_commands_action_check
    CHECK (action IN ('cut', 'restore', 'timer', 'param'));

CREATE INDEX IF NOT EXISTS device_commands_device_idx
    ON device_commands (device_id, id DESC);
CREATE INDEX IF NOT EXISTS device_commands_queued_idx
    ON device_commands (device_id) WHERE status = 'queued';

CREATE OR REPLACE FUNCTION notify_device_command() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('device_command', NEW.device_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS device_commands_notify ON device_commands;
CREATE TRIGGER device_commands_notify
    AFTER INSERT ON device_commands
    FOR EACH ROW EXECUTE FUNCTION notify_device_command();

-- Whether engine cut-off is switched on for a device -- an admin's choice,
-- per device, off until switched on (no row = off). Only a cut needs it:
-- restoring fuel is always allowed, or switching it off right after a cut
-- would leave the vehicle stranded.
CREATE TABLE IF NOT EXISTS device_relay (
    device_id  TEXT        PRIMARY KEY,
    enabled    BOOLEAN     NOT NULL DEFAULT false,
    changed_by BIGINT      REFERENCES users(id) ON DELETE SET NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The gateway's one entry point: settle what can no longer be sent, then
-- hand over (and mark sent) what can, oldest first. SKIP LOCKED so two
-- gateway processes never both take the same command.
CREATE OR REPLACE FUNCTION claim_device_commands(p_device_id TEXT)
RETURNS SETOF device_commands AS $$
BEGIN
    UPDATE device_commands
    SET status = 'expired', completed_at = now()
    WHERE device_id = p_device_id AND status = 'queued' AND expires_at <= now();

    UPDATE device_commands
    SET status = 'failed', error = 'relay_disabled', completed_at = now()
    WHERE device_id = p_device_id AND status = 'queued' AND action = 'cut'
      AND NOT EXISTS (
          SELECT 1 FROM device_relay r
          WHERE r.device_id = p_device_id AND r.enabled
      );

    UPDATE device_commands
    SET status = 'failed', error = 'ignition_on', completed_at = now()
    WHERE device_id = p_device_id AND status = 'queued' AND action = 'cut'
      AND NOT EXISTS (
          SELECT 1 FROM device_status s
          WHERE s.device_id = p_device_id AND s.ignition = false
      );

    RETURN QUERY
    UPDATE device_commands c
    SET status = 'sent', sent_at = now()
    WHERE c.id IN (
        SELECT q.id FROM device_commands q
        WHERE q.device_id = p_device_id AND q.status = 'queued'
        ORDER BY q.id
        FOR UPDATE SKIP LOCKED
    )
    RETURNING c.*;
END;
$$ LANGUAGE plpgsql;

-- Same ownership fix as the geofence grants above: the API writes commands,
-- the gateway claims and completes them.
DO $grant_device_commands$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT DISTINCT who FROM (
            SELECT c.relowner::regrole::text AS who
            FROM pg_class c WHERE c.relname = 'device_locations'
            UNION
            SELECT a.grantee::regrole::text
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.relname = 'device_locations' AND a.privilege_type = 'INSERT' AND a.grantee <> 0
        ) AS writers
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON device_commands TO %s', r.who);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON device_relay TO %s', r.who);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE device_commands_id_seq TO %s', r.who);
    END LOOP;
END
$grant_device_commands$;


-- ---------------------------------------------------------------------------
-- Tracker allowlist: which IMEIs the gateway accepts at login.
--
-- GT06 has no authentication -- a tracker is whatever IMEI it announces --
-- so this cannot stop someone who knows one of *these* IMEIs. What it does
-- stop is every IMEI nobody approved: scanners, garbage, guessed ids, and
-- anything that would otherwise write rows or receive commands.
--
-- - Created and seeded once, with every device that already reported or is
--   owned, so turning this on breaks nothing. Seeding only on creation means
--   re-running this file never re-adds a device an admin removed.
-- - A device someone owns is allowed: the trigger on user_devices covers a
--   customer's self-claim and an admin's assignment alike.
-- - An unknown IMEI's login is refused and counted in device_login_attempts,
--   where an admin can approve it (which clears the attempt).

DO $device_allowlist$
BEGIN
    IF to_regclass('device_allowlist') IS NULL THEN
        CREATE TABLE device_allowlist (
            device_id TEXT        PRIMARY KEY,
            added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            added_by  BIGINT      REFERENCES users(id) ON DELETE SET NULL,
            -- 'existing': seeded when the allowlist was created; 'owner':
            -- someone claimed or was assigned it; 'admin': approved by hand;
            -- 'auto': admitted while auto-approve was on (gateway_settings).
            source    TEXT        NOT NULL DEFAULT 'admin'
                      CHECK (source IN ('existing', 'owner', 'admin', 'auto'))
        );
        INSERT INTO device_allowlist (device_id, source)
        SELECT device_id, 'existing' FROM (
            SELECT DISTINCT device_id FROM device_locations
            UNION
            SELECT device_id FROM user_devices
        ) known
        ON CONFLICT DO NOTHING;
    END IF;
END
$device_allowlist$;

-- Widened for databases whose allowlist predates 'auto'.
ALTER TABLE device_allowlist DROP CONSTRAINT IF EXISTS device_allowlist_source_check;
ALTER TABLE device_allowlist ADD CONSTRAINT device_allowlist_source_check
    CHECK (source IN ('existing', 'owner', 'admin', 'auto'));

-- The admin's choice for trackers nobody approved yet -- one row:
-- - auto_approve off (default, "hold"): refused and listed in
--   device_login_attempts until an admin approves them
-- - auto_approve on: admitted at once and added to the allowlist as
--   'auto', so switching back to hold later keeps them (remove one by hand
--   to refuse it again)
CREATE TABLE IF NOT EXISTS gateway_settings (
    id           BOOLEAN     PRIMARY KEY DEFAULT true CHECK (id),
    auto_approve BOOLEAN     NOT NULL DEFAULT false,
    changed_by   BIGINT      REFERENCES users(id) ON DELETE SET NULL,
    changed_at   TIMESTAMPTZ
);
INSERT INTO gateway_settings (id) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS device_login_attempts (
    device_id  TEXT        PRIMARY KEY,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts   INTEGER     NOT NULL DEFAULT 1,
    -- Where the newest attempt came from (ip:port), for telling a real
    -- tracker on a mobile network from a scanner in a data centre.
    last_peer  TEXT
);

CREATE OR REPLACE FUNCTION allow_owned_device() RETURNS trigger AS $$
BEGIN
    INSERT INTO device_allowlist (device_id, source)
    VALUES (NEW.device_id, 'owner')
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_devices_allow ON user_devices;
CREATE TRIGGER user_devices_allow
    AFTER INSERT ON user_devices
    FOR EACH ROW EXECUTE FUNCTION allow_owned_device();

CREATE OR REPLACE FUNCTION clear_login_attempts() RETURNS trigger AS $$
BEGIN
    DELETE FROM device_login_attempts WHERE device_id = NEW.device_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS device_allowlist_clear_attempts ON device_allowlist;
CREATE TRIGGER device_allowlist_clear_attempts
    AFTER INSERT ON device_allowlist
    FOR EACH ROW EXECUTE FUNCTION clear_login_attempts();

-- The gateway's one call at login: true if allowed -- or newly allowed,
-- while auto-approve is on -- otherwise the attempt is counted and false
-- returned.
CREATE OR REPLACE FUNCTION gateway_admit(p_device_id TEXT, p_peer TEXT)
RETURNS BOOLEAN AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM device_allowlist WHERE device_id = p_device_id) THEN
        RETURN true;
    END IF;
    IF EXISTS (SELECT 1 FROM gateway_settings WHERE auto_approve) THEN
        -- The insert trigger clears any earlier refused attempt.
        INSERT INTO device_allowlist (device_id, source)
        VALUES (p_device_id, 'auto')
        ON CONFLICT DO NOTHING;
        RETURN true;
    END IF;
    INSERT INTO device_login_attempts AS a (device_id, last_peer)
    VALUES (p_device_id, p_peer)
    ON CONFLICT (device_id) DO UPDATE
    SET last_seen = now(), attempts = a.attempts + 1, last_peer = EXCLUDED.last_peer;
    RETURN false;
END;
$$ LANGUAGE plpgsql;

DO $grant_allowlist$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT DISTINCT who FROM (
            SELECT c.relowner::regrole::text AS who
            FROM pg_class c WHERE c.relname = 'device_locations'
            UNION
            SELECT a.grantee::regrole::text
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.relname = 'device_locations' AND a.privilege_type = 'INSERT' AND a.grantee <> 0
        ) AS writers
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON device_allowlist, device_login_attempts, '
            'gateway_settings TO %s',
            r.who);
    END LOOP;
END
$grant_allowlist$;


-- ---------------------------------------------------------------------------
-- Device shop: trackers on sale, and customers' orders for them.
--
-- Payment is cash on delivery only -- no payment gateway is wired up (each
-- charges a fee per payment). An admin moves an order placed -> confirmed
-- -> shipped -> delivered (or cancels it); the customer may cancel while it
-- is still placed. Entering the shipped trackers' IMEIs on a shipped or
-- delivered order claims them for the customer (device_claims), so the
-- tracker turns up in their app without them typing the IMEI.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_products (
    id          BIGSERIAL     PRIMARY KEY,
    name        TEXT          NOT NULL,
    description TEXT,
    -- Rupees. NUMERIC, not float: money.
    price       NUMERIC(10,2) NOT NULL CHECK (price >= 0),
    -- Hidden from customers when false; kept, because orders refer to it.
    active      BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS device_orders (
    id             BIGSERIAL     PRIMARY KEY,
    user_id        BIGINT        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    product_id     BIGINT        NOT NULL REFERENCES device_products (id),
    -- The product's name and price when ordered: a later price change must
    -- not rewrite what the customer agreed to pay.
    product_name   TEXT          NOT NULL,
    unit_price     NUMERIC(10,2) NOT NULL,
    quantity       INTEGER       NOT NULL CHECK (quantity BETWEEN 1 AND 10),
    contact_name   TEXT          NOT NULL,
    phone          TEXT          NOT NULL,
    address        TEXT          NOT NULL,
    city           TEXT          NOT NULL,
    state          TEXT,
    pincode        TEXT          NOT NULL,
    notes          TEXT,
    payment_method TEXT          NOT NULL DEFAULT 'cod' CHECK (payment_method IN ('cod')),
    status         TEXT          NOT NULL DEFAULT 'placed'
                   CHECK (status IN ('placed', 'confirmed', 'shipped', 'delivered', 'cancelled')),
    tracking       TEXT,
    admin_note     TEXT,
    -- IMEIs of the trackers sent; claimed for the customer when set on a
    -- shipped or delivered order.
    device_ids     TEXT[]        NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS device_orders_user_idx ON device_orders (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS device_orders_status_idx ON device_orders (status, created_at DESC);

-- Every status change, for the timeline the customer sees.
CREATE TABLE IF NOT EXISTS device_order_events (
    id         BIGSERIAL   PRIMARY KEY,
    order_id   BIGINT      NOT NULL REFERENCES device_orders (id) ON DELETE CASCADE,
    status     TEXT        NOT NULL,
    note       TEXT,
    changed_by BIGINT      REFERENCES users (id) ON DELETE SET NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS device_order_events_order_idx
    ON device_order_events (order_id, changed_at);

-- Same as the geofence tables: this file is applied as a superuser, so the
-- shop tables end up owned by it and closed to the API's own user, and
-- every product or order request would fail with "permission denied". The
-- API's user (device_locations' owner, or anyone granted INSERT on it) gets
-- them. Re-running this is harmless.
DO $grant_shop$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT DISTINCT who FROM (
            SELECT c.relowner::regrole::text AS who
            FROM pg_class c WHERE c.relname = 'device_locations'
            UNION
            SELECT a.grantee::regrole::text
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.relname = 'device_locations' AND a.privilege_type = 'INSERT' AND a.grantee <> 0
        ) AS writers
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON device_products, device_orders, '
            'device_order_events TO %s', r.who);
        EXECUTE format(
            'GRANT USAGE, SELECT ON SEQUENCE device_products_id_seq, device_orders_id_seq, '
            'device_order_events_id_seq TO %s', r.who);
    END LOOP;
END
$grant_shop$;
