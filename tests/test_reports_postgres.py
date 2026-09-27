"""
The Postgres trip report must agree with api/reports.py, fix for fix.

Needs a real database, so it only runs with TEST_PG_DSN pointed at a
scratch one that has sql/schema.sql applied -- the rest of the suite stays
database-free. It writes to device_locations under its own device ids and
deletes them afterwards.

    TEST_PG_DSN=postgresql://... python -m unittest tests.test_reports_postgres
"""

import os
import random
import unittest
from datetime import datetime, timedelta, timezone

from api.reports import ReportFix, fix_time, summarize_device

DSN = os.environ.get("TEST_PG_DSN")
PREFIX = "report-parity-"


@unittest.skipUnless(DSN, "TEST_PG_DSN is not set")
class TestTripReportParity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        from api.repository import PostgresLocationRepository

        self.pool = await asyncpg.create_pool(DSN)
        self.repo = PostgresLocationRepository(self.pool)
        await self.pool.execute("DELETE FROM device_locations WHERE device_id LIKE $1", PREFIX + "%")

    async def asyncTearDown(self):
        await self.pool.execute("DELETE FROM device_locations WHERE device_id LIKE $1", PREFIX + "%")
        await self.pool.close()

    async def test_sql_matches_the_python_rules_on_random_trips(self):
        rng = random.Random(20260927)
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        expected = {}
        rows = []
        for n in range(6):
            device_id = f"{PREFIX}{n}"
            at, lat, lng = start, 12.9 + n / 100, 77.5
            fixes = []
            for _ in range(400):
                # Drives, stops of every length (some past 10 min), dropouts.
                moving = rng.random() < 0.55
                speed = rng.choice([None, 0]) if not moving else rng.randint(1, 90)
                # The junk a real tracker sends: no GPS lock, one-byte
                # garbage near 255, and isolated spikes.
                gps_fixed = rng.choice([True, True, True, True, False, None])
                if moving and rng.random() < 0.05:
                    speed = rng.choice([rng.randint(150, 199), rng.randint(200, 255)])
                at += timedelta(seconds=rng.choice([10, 30, 60, 60, 120, 900]))
                if moving:
                    lat += rng.uniform(-0.002, 0.002)
                    lng += rng.uniform(-0.002, 0.002)
                # When it reached us: mostly on time, sometimes a buffered
                # backlog arriving much later, now and then no tracker time
                # at all, or a tracker clock far enough off to be ignored.
                fixed_at = at
                received_at = at + timedelta(seconds=rng.choice([1, 2, 5, 5, 5, 600, 3600]))
                clock = rng.random()
                if clock < 0.05:
                    fixed_at = None
                elif clock < 0.08:
                    fixed_at = received_at + timedelta(minutes=rng.choice([11, 90]))
                elif clock < 0.10:
                    fixed_at = received_at - timedelta(days=45)
                effective = fix_time(fixed_at, received_at)
                fixes.append((effective, len(fixes), ReportFix(lat, lng, speed, effective, gps_fixed)))
                rows.append((device_id, lat, lng, speed, fixed_at, received_at, gps_fixed))
            # Rows are inserted in this order, so insertion order is id order.
            fixes.sort(key=lambda f: (f[0], f[1]))
            expected[device_id] = summarize_device([f[2] for f in fixes])
        await self.pool.executemany(
            "INSERT INTO device_locations "
            "(device_id, latitude, longitude, speed_kmh, fixed_at, received_at, gps_fixed) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            rows,
        )

        reports = await self.repo.trip_report(
            start, start + timedelta(days=31), device_ids=frozenset(expected)
        )

        self.assertEqual(sorted(r.device_id for r in reports), sorted(expected))
        for report in reports:
            want = expected[report.device_id]
            with self.subTest(device=report.device_id):
                self.assertAlmostEqual(report.distance_km, want.distance_km, places=6)
                self.assertEqual(report.max_speed_kmh, want.max_speed_kmh)
                self.assertAlmostEqual(report.running_minutes, want.running_minutes, places=6)
                self.assertEqual(report.halt_count, want.halt_count)
                self.assertAlmostEqual(report.halt_minutes, want.halt_minutes, places=6)
                self.assertAlmostEqual(report.longest_halt_minutes, want.longest_halt_minutes, places=6)
                self.assertEqual(report.fix_count, want.fix_count)
                self.assertEqual(report.first_fix_at, want.first_fix_at)
                self.assertEqual(report.last_fix_at, want.last_fix_at)
        self.assertTrue(any(r.halt_count for r in reports), "the sample should contain halts")


if __name__ == "__main__":
    unittest.main()
