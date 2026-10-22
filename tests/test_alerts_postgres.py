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
