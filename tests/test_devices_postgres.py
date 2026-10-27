"""
The Postgres device list must give the same answers as reading every row.

list_devices answers from index probes (one distinct device at a time, the
newest fix, the newest moving fix, the first fix) so it stays cheap as
device_locations grows; this checks those probes against the plain Python
reading of the same rows. Needs a scratch database with sql/schema.sql
applied, so it only runs with TEST_PG_DSN set:

    TEST_PG_DSN=postgresql://... python -m unittest tests.test_devices_postgres
"""

import os
import random
import unittest
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("TEST_PG_DSN")
PREFIX = "devlist-"


@unittest.skipUnless(DSN, "TEST_PG_DSN is not set")
class TestDeviceListProbes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        from api.repository import PostgresLocationRepository

        self.pool = await asyncpg.create_pool(DSN)
        self.repo = PostgresLocationRepository(self.pool)
        await self._clean()

    async def asyncTearDown(self):
        await self._clean()
        await self.pool.close()

    async def _clean(self):
        for table in ("device_locations", "device_status"):
            await self.pool.execute(f"DELETE FROM {table} WHERE device_id LIKE $1", PREFIX + "%")

    async def insert(self, device, at, speed, fixed_at=None):
        await self.pool.execute(
            "INSERT INTO device_locations (device_id, latitude, longitude, speed_kmh, received_at, fixed_at) "
            "VALUES ($1, 12.9, 77.6, $2, $3, $4)",
            PREFIX + device, speed, at, fixed_at or at,
        )

    async def listed(self, ids=None):
        rows = await self.repo.list_devices(ids)
        return {r.device_id: r for r in rows if r.device_id.startswith(PREFIX)}

    async def test_last_seen_count_and_halted_since_match_the_rows(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rng = random.Random(7)
        expected = {}
        for n in range(5):
            device = f"d{n}"
            rows = []
            at = now - timedelta(hours=40)
            for _ in range(120):
                at += timedelta(minutes=rng.choice([2, 10, 25]))
                if at > now:
                    break
                speed = rng.choice([0, 0, 0, 25, 40])
                rows.append((at, speed))
                await self.insert(device, at, speed)
            moving = [t for t, s in rows if s > 0]
            expected[PREFIX + device] = {
                "last_seen": rows[-1][0],
                "fix_count": sum(1 for t, _ in rows if t > now - timedelta(hours=24)),
                "halted_since": max(moving) if moving else rows[0][0],
            }
        got = await self.listed()
        self.assertEqual(set(got), set(expected))
        for device_id, want in expected.items():
            self.assertEqual(got[device_id].last_seen, want["last_seen"], device_id)
            self.assertEqual(got[device_id].halted_since, want["halted_since"], device_id)
            # The count is taken a moment after `now` above, so allow rows that aged out meanwhile.
            self.assertAlmostEqual(got[device_id].fix_count, want["fix_count"], delta=1, msg=device_id)

    async def test_a_vehicle_that_never_moved_is_halted_since_its_first_fix(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        await self.insert("still", now - timedelta(hours=3), 0)
        await self.insert("still", now - timedelta(hours=1), 0)
        got = (await self.listed())[PREFIX + "still"]
        self.assertEqual(got.halted_since, now - timedelta(hours=3))
        self.assertEqual(got.last_seen, now - timedelta(hours=1))

    async def test_the_fix_count_is_the_last_day_only(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        await self.insert("old", now - timedelta(days=5), 10)
        await self.insert("old", now - timedelta(hours=2), 0)
        await self.insert("old", now - timedelta(hours=1), 0)
        self.assertEqual((await self.listed())[PREFIX + "old"].fix_count, 2)

    async def test_narrowing_to_some_devices_and_a_device_with_no_fixes(self):
        now = datetime.now(timezone.utc)
        await self.insert("a", now, 10)
        await self.insert("b", now, 10)
        got = await self.listed(frozenset({PREFIX + "a", PREFIX + "never-reported"}))
        self.assertEqual(set(got), {PREFIX + "a"})
