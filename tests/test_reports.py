"""Trip report rules (api/reports.py) and the /stats/report endpoint."""

import unittest
from datetime import datetime, timedelta, timezone

from api.reports import ReportFix, haversine_m, plausible_top_speed, summarize_device

try:
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
BASE = "/api/v1"
API_KEY = "test-secret"
ADMIN = {"username": "admin", "password": "adminpassword"}


def fix(minute: float, speed: int | None, lat: float = 12.97, lng: float = 77.59) -> ReportFix:
    return ReportFix(lat, lng, speed, T0 + timedelta(minutes=minute))


class TestSummarizeDevice(unittest.TestCase):
    def test_empty_window(self):
        trips = summarize_device([])
        self.assertEqual((trips.distance_km, trips.fix_count, trips.halt_count), (0, 0, 0))
        self.assertIsNone(trips.max_speed_kmh)
        self.assertIsNone(trips.first_fix_at)

    def test_distance_sums_consecutive_legs(self):
        # Two 0.01-degree hops north: ~1.11 km each.
        trips = summarize_device([fix(0, 40), fix(1, 40, lat=12.98), fix(2, 40, lat=12.99)])
        expected = 2 * haversine_m(12.97, 77.59, 12.98, 77.59) / 1000
        self.assertAlmostEqual(trips.distance_km, expected, places=6)
        self.assertAlmostEqual(trips.distance_km, 2.22, places=2)

    def test_max_speed_ignores_missing_speeds(self):
        trips = summarize_device([fix(0, None), fix(1, 55), fix(2, 30)])
        self.assertEqual(trips.max_speed_kmh, 55)

    def test_top_speed_ignores_a_junk_reading_over_the_ceiling(self):
        # GT06 speed is one byte; junk arrives as 240-255.
        trips = summarize_device([fix(0, 60), fix(1, 240), fix(2, 238), fix(3, 62)])
        self.assertEqual(trips.max_speed_kmh, 62)

    def test_top_speed_ignores_readings_without_a_gps_lock(self):
        fixes = [fix(0, 40), ReportFix(12.97, 77.59, 150, T0 + timedelta(minutes=1), False), fix(2, 45)]
        self.assertEqual(plausible_top_speed(fixes), 45)

    def test_top_speed_ignores_an_isolated_spike(self):
        # 40 -> 170 -> 40 in consecutive fixes is a glitch, not a sprint.
        self.assertEqual(plausible_top_speed([fix(0, 40), fix(1, 170), fix(2, 40)]), 40)

    def test_top_speed_keeps_a_real_ramp_up(self):
        # Each step is within 50 km/h of a neighbour, so 150 is believed.
        self.assertEqual(plausible_top_speed([fix(0, 60), fix(1, 110), fix(2, 150), fix(3, 140)]), 150)

    def test_a_spike_at_the_edge_is_judged_by_its_one_neighbour(self):
        self.assertEqual(plausible_top_speed([fix(0, 190), fix(1, 30), fix(2, 35)]), 35)
        self.assertEqual(plausible_top_speed([fix(0, 100)]), 100)

    def test_running_counts_gaps_after_a_moving_fix(self):
        # Moving 0->1 and 1->2 (2 min), stopped from 2 onwards.
        trips = summarize_device([fix(0, 30), fix(1, 30), fix(2, 0), fix(3, 0)])
        self.assertAlmostEqual(trips.running_minutes, 2)

    def test_a_long_silence_is_not_running_time(self):
        # 45 minutes without a fix after a moving one: the tracker went quiet.
        trips = summarize_device([fix(0, 30), fix(1, 30), fix(46, 30), fix(47, 30)])
        self.assertAlmostEqual(trips.running_minutes, 2)

    def test_halt_needs_ten_minutes_and_two_fixes(self):
        fixes = (
            [fix(0, 30)]
            + [fix(m, 0) for m in (1, 3, 5)]  # a 4-minute stop: traffic, not a halt
            + [fix(6, 30)]
            + [fix(m, 0) for m in range(7, 22)]  # a 14-minute halt
            + [fix(22, 30), fix(23, 0)]  # a single stationary fix: no stop at all
        )
        trips = summarize_device(fixes)
        self.assertEqual(trips.halt_count, 1)
        self.assertAlmostEqual(trips.halt_minutes, 14)
        self.assertAlmostEqual(trips.longest_halt_minutes, 14)

    def test_halts_add_up_and_the_longest_is_kept(self):
        fixes = (
            [fix(m, 0) for m in range(0, 11)]  # 10 minutes: exactly the threshold
            + [fix(11, 20)]
            + [fix(m, 0) for m in range(12, 42)]  # 29 minutes, still going at the window end
        )
        trips = summarize_device(fixes)
        self.assertEqual(trips.halt_count, 2)
        self.assertAlmostEqual(trips.halt_minutes, 39)
        self.assertAlmostEqual(trips.longest_halt_minutes, 29)

    def test_a_missing_speed_counts_as_stopped(self):
        trips = summarize_device([fix(m, None) for m in range(0, 12)])
        self.assertEqual(trips.halt_count, 1)
        self.assertAlmostEqual(trips.halt_minutes, 11)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestReportEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(
            ApiConfig(
                backend="memory",
                ingest_api_key=API_KEY,
                bootstrap_admin_username=ADMIN["username"],
                bootstrap_admin_password=ADMIN["password"],
            )
        )
        self.client = AsgiClient(self.app)
        await self.client.__aenter__()
        login = await self.client.post(f"{BASE}/auth/login", json=ADMIN)
        self.admin = {"Authorization": f"Bearer {login.json()['token']}"}
        await self.client.post(
            f"{BASE}/users",
            json={"username": "owner", "password": "owner1234", "role": "user", "devices": ["dev1"]},
            headers=self.admin,
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "owner", "password": "owner1234"}
        )
        self.owner = {"Authorization": f"Bearer {login.json()['token']}"}
        for device_id, lat, speed in (("dev1", 12.97, 30), ("dev1", 12.98, 45), ("dev2", 13.0, 60)):
            await self.client.post(
                f"{BASE}/ingest/location",
                json={"device_id": device_id, "latitude": lat, "longitude": 77.59, "speed_kmh": speed},
                headers={"X-API-Key": API_KEY},
            )

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def test_admin_gets_every_vehicle_and_fleet_totals(self):
        response = await self.client.get(f"{BASE}/stats/report", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["halt_threshold_minutes"], 10)
        self.assertEqual([d["device_id"] for d in body["devices"]], ["dev1", "dev2"])
        dev1 = body["devices"][0]
        self.assertAlmostEqual(dev1["distance_km"], 1.11, places=2)
        self.assertEqual((dev1["max_speed_kmh"], dev1["fix_count"]), (45, 2))
        self.assertEqual(body["totals"]["devices"], 2)
        self.assertEqual(body["totals"]["max_speed_kmh"], 60)
        self.assertAlmostEqual(body["totals"]["distance_km"], dev1["distance_km"])

    async def test_a_user_sees_only_their_own_vehicles(self):
        body = (await self.client.get(f"{BASE}/stats/report", headers=self.owner)).json()
        self.assertEqual([d["device_id"] for d in body["devices"]], ["dev1"])
        self.assertEqual(body["totals"]["max_speed_kmh"], 45)

    async def test_a_window_before_any_fix_is_empty(self):
        response = await self.client.get(
            f"{BASE}/stats/report?since=2020-01-01T00:00:00Z&until=2020-01-02T00:00:00Z",
            headers=self.admin,
        )
        body = response.json()
        self.assertEqual(body["devices"], [])
        self.assertEqual(body["totals"]["distance_km"], 0)
        self.assertIsNone(body["totals"]["max_speed_kmh"])

    async def test_rejects_a_backwards_or_oversized_window(self):
        backwards = await self.client.get(
            f"{BASE}/stats/report?since=2026-09-02T00:00:00Z&until=2026-09-01T00:00:00Z",
            headers=self.admin,
        )
        too_long = await self.client.get(
            f"{BASE}/stats/report?since=2026-01-01T00:00:00Z&until=2026-03-01T00:00:00Z",
            headers=self.admin,
        )
        self.assertEqual((backwards.status_code, too_long.status_code), (422, 422))

    async def test_requires_sign_in(self):
        self.assertEqual((await self.client.get(f"{BASE}/stats/report")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
