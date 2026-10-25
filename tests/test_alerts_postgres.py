"""
The alerts history on a real Postgres: the triggers write the rows the rules
say they should (a pause is not a stop; overspeed once per crossing; harsh
braking; ignition flips; a geofence crossing that alerts), and the SQL
behind GET /alerts scopes, filters and pages the same way the in-memory
repository does.

Only runs with TEST_PG_DSN pointed at a scratch database that has
sql/schema.sql applied; it writes under its own device ids and removes them.

    TEST_PG_DSN=postgresql://... python -m unittest tests.test_alerts_postgres
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("TEST_PG_DSN")
PREFIX = "alert-test-"
T0 = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


@unittest.skipUnless(DSN, "TEST_PG_DSN is not set")
class TestAlertTriggers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        from api.alerts_repository import PostgresAlertRepository

        self.pool = await asyncpg.create_pool(DSN)
        self.repo = PostgresAlertRepository(self.pool)
        await self._clean()

    async def asyncTearDown(self):
        await self._clean()
        await self.pool.close()

    async def _clean(self):
        for table in ("vehicle_alerts", "device_locations", "device_status", "device_ignition_log",
                      "device_speed_limits"):
            await self.pool.execute(f"DELETE FROM {table} WHERE device_id LIKE $1", PREFIX + "%")

    async def fixes(self, device, rows):
        """rows: (seconds, speed_kmh[, satellites])"""
        for row in rows:
            seconds, speed, sats = (*row, 8)[:3]
            await self.pool.execute(
                "INSERT INTO device_locations (device_id, latitude, longitude, speed_kmh, satellites, gps_fixed, received_at) "
                "VALUES ($1, 12.9, 77.6, $2, $3, true, $4)",
                PREFIX + device, speed, sats, T0 + timedelta(seconds=seconds),
            )

    async def kinds(self, device):
        rows = await self.pool.fetch(
            "SELECT kind FROM vehicle_alerts WHERE device_id = $1 ORDER BY occurred_at, id", PREFIX + device
        )
        return [r["kind"] for r in rows]

    async def test_a_pause_is_not_a_stop_but_three_minutes_is(self):
        await self.fixes("pause", [(0, 40), (30, 40), (60, 0), (120, 0), (150, 30)])
        self.assertEqual(await self.kinds("pause"), [])
        await self.fixes("stop", [(0, 40), (60, 0), (120, 0), (180, 0), (240, 0), (300, 0)])
        self.assertEqual(await self.kinds("stop"), ["stopped"])  # once, not at every stopped fix

    async def stamp(self, device, rows):
        """rows: (minutes ago, speed) -- fixes at fixed times before now."""
        for ago, speed in rows:
            await self.pool.execute(
                "INSERT INTO device_locations (device_id, latitude, longitude, speed_kmh, satellites, gps_fixed, received_at) "
                "VALUES ($1, 12.9, 77.6, $2, 8, true, now() - make_interval(secs => $3))",
                PREFIX + device, speed, float(ago * 60),
            )

    async def test_a_stop_is_announced_by_the_clock_without_a_new_fix(self):
        # Moving until 6 min ago, one stationary fix at 5.5 min ago, then the tracker is quiet.
        await self.stamp("quiet", [(8, 40), (6, 30), (5.5, 0)])
        self.assertEqual(await self.kinds("quiet"), [])  # the trigger saw no fix three minutes on
        n = await self.pool.fetchval("SELECT announce_due_stops()")
        self.assertGreaterEqual(n, 1)
        self.assertEqual(await self.kinds("quiet"), ["stopped"])
        # Once: asking again announces nothing, and no row is added.
        await self.pool.fetchval("SELECT announce_due_stops()")
        self.assertEqual(await self.kinds("quiet"), ["stopped"])
        # It is stamped at last-moving + 3 minutes, not at the time it was found.
        gap = await self.pool.fetchval(
            "SELECT (SELECT occurred_at FROM vehicle_alerts WHERE device_id = $1 AND kind = 'stopped') - "
            "(SELECT max(received_at) FROM device_locations WHERE device_id = $1 AND speed_kmh > 0)", PREFIX + "quiet",
        )
        self.assertEqual(gap.total_seconds(), 180)

    async def test_not_before_three_minutes_and_not_while_moving(self):
        await self.stamp("early", [(4, 40), (2, 30), (1.5, 0)])  # moved 2 min ago: not yet
        await self.stamp("rolling", [(10, 0), (5, 30), (0.1, 40)])  # still going
        await self.stamp("old", [(60, 40), (59, 0)])  # a stop an hour ago is not announced now
        await self.pool.fetchval("SELECT announce_due_stops()")
        for device in ("early", "rolling", "old"):
            # ("rolling" did raise "moving": it was parked for ten minutes first.)
            self.assertNotIn("stopped", await self.kinds(device), device)

    async def test_a_fix_after_the_clock_announced_it_does_not_announce_it_again(self):
        await self.stamp("both", [(8, 40), (6, 30), (5.5, 0)])
        await self.pool.fetchval("SELECT announce_due_stops()")
        await self.stamp("both", [(0, 0)])  # the parked tracker's next report
        self.assertEqual(await self.kinds("both"), ["stopped"])
        # ...and the other way round: the trigger got there first.
        await self.stamp("trigger", [(10, 40), (9, 30), (8, 0), (5, 0), (0.2, 0)])
        before = await self.kinds("trigger")
        await self.pool.fetchval("SELECT announce_due_stops()")
        self.assertEqual(await self.kinds("trigger"), before)
        self.assertEqual(before, ["stopped"])

    async def test_overspeed_once_per_crossing_and_harsh_driving(self):
        await self.fixes("drive", [(0, 70), (10, 95), (20, 96), (30, 20), (40, 70), (50, 0, 0)])
        self.assertEqual(
            await self.kinds("drive"), ["overspeed", "harsh_braking", "harsh_acceleration"]
        )

    async def test_the_limit_is_per_vehicle(self):
        await self.repo.set_speed_limit(PREFIX + "limit", 50)
        await self.fixes("limit", [(0, 40), (10, 60)])
        self.assertEqual(await self.kinds("limit"), ["overspeed"])

    async def test_ignition_flips_but_not_the_first_report(self):
        device = PREFIX + "ign"
        await self.pool.execute("SELECT record_ignition($1, false, $2)", device, T0)
        await self.pool.execute("SELECT record_ignition($1, true, $2)", device, T0 + timedelta(minutes=5))
        self.assertEqual(await self.kinds("ign"), ["ignition_on"])

    async def test_an_ignition_alert_is_placed_where_the_vehicle_last_was(self):
        await self.fixes("here", [(0, 30), (60, 0)])
        device = PREFIX + "here"
        await self.pool.execute("SELECT record_ignition($1, true, $2)", device, T0 + timedelta(seconds=90))
        await self.pool.execute("SELECT record_ignition($1, false, $2)", device, T0 + timedelta(minutes=5))
        row = await self.pool.fetchrow(
            "SELECT latitude, longitude FROM vehicle_alerts WHERE device_id = $1 AND kind = 'ignition_off'", device
        )
        self.assertEqual((row["latitude"], row["longitude"]), (12.9, 77.6))

    async def test_an_ignition_alert_with_no_fix_has_no_place_rather_than_failing(self):
        device = PREFIX + "nowhere"
        await self.pool.execute("SELECT record_ignition($1, true, $2)", device, T0)
        await self.pool.execute("SELECT record_ignition($1, false, $2)", device, T0 + timedelta(minutes=5))
        row = await self.pool.fetchrow(
            "SELECT latitude, longitude FROM vehicle_alerts WHERE device_id = $1 AND kind = 'ignition_off'", device
        )
        self.assertEqual((row["latitude"], row["longitude"]), (None, None))

    async def test_reading_scopes_filters_and_marks_read(self):
        from api.alerts_repository import AlertViewer

        await self.fixes("read", [(0, 70), (10, 95)])
        mine = AlertViewer(user_id=-1, is_admin=False, devices=frozenset({PREFIX + "read"}), can_use_geofences=False)
        other = AlertViewer(user_id=-2, is_admin=False, devices=frozenset({PREFIX + "elsewhere"}), can_use_geofences=False)
        entries, total = await self.repo.list_alerts(mine)
        self.assertEqual((total, entries[0].kind), (1, "overspeed"))
        self.assertEqual((await self.repo.list_alerts(other))[1], 0)
        self.assertEqual((await self.repo.list_alerts(mine, kinds=("stopped",)))[1], 0)
        count, seen = await self.repo.unread(mine)
        self.assertEqual((count, seen), (1, 0))
        await self.repo.mark_seen(mine, entries[0].id)
        await self.repo.mark_seen(mine, 0)  # never back
        self.assertEqual(await self.repo.unread(mine), (0, entries[0].id))
        await self.pool.execute("DELETE FROM alert_reads WHERE user_id < 0")


if __name__ == "__main__":
    unittest.main()
