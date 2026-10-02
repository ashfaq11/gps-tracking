"""Trip report rules (api/reports.py) and the /stats/report endpoint."""

import unittest
from datetime import datetime, timedelta, timezone

from api.reports import (
    ReportFix,
    clean_route,
    fix_time,
    haversine_m,
    plausible_top_speed,
    summarize_device,
)

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
        fixes = [
            fix(0, 40),
            ReportFix(12.97, 77.59, 150, T0 + timedelta(minutes=1), False),
            fix(2, 45),
        ]
        self.assertEqual(plausible_top_speed(fixes), 45)

    def test_top_speed_ignores_an_isolated_spike(self):
        # 40 -> 170 -> 40 in consecutive fixes is a glitch, not a sprint.
        self.assertEqual(plausible_top_speed([fix(0, 40), fix(1, 170), fix(2, 40)]), 40)

    def test_top_speed_keeps_a_real_ramp_up(self):
        # Each step is within 50 km/h of a neighbour, so 150 is believed.
        self.assertEqual(
            plausible_top_speed([fix(0, 60), fix(1, 110), fix(2, 150), fix(3, 140)]), 150
        )

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

    def test_a_halt_needs_ten_minutes_and_lasts_until_it_moves(self):
        fixes = (
            [fix(0, 30)]
            + [fix(m, 0) for m in (1, 3, 5)]  # stopped 1 -> 6: a 5-minute short stop
            + [fix(6, 30)]
            + [fix(m, 0) for m in range(7, 22)]  # stopped 7 -> 22: a 15-minute halt
            + [fix(22, 30), fix(23, 0)]
        )
        trips = summarize_device(fixes)
        self.assertEqual(trips.halt_count, 1)
        self.assertAlmostEqual(trips.halt_minutes, 15)
        self.assertAlmostEqual(trips.longest_halt_minutes, 15)
        self.assertAlmostEqual(trips.short_stop_minutes, 5)
        self.assertAlmostEqual(trips.running_minutes, 3)
        self.assertAlmostEqual(trips.no_data_minutes, 0)

    def test_halts_add_up_and_the_longest_is_kept(self):
        fixes = (
            [fix(m, 0) for m in range(0, 11)]  # 10 minutes: exactly the threshold
            + [fix(11, 20)]
            + [fix(m, 0) for m in range(12, 42)]  # 29 minutes, still going at the window end
        )
        trips = summarize_device(fixes)
        # 0 -> 11 (until it moved) and 12 -> 41.
        self.assertEqual(trips.halt_count, 2)
        self.assertAlmostEqual(trips.halt_minutes, 40)
        self.assertAlmostEqual(trips.longest_halt_minutes, 29)

    def test_a_missing_speed_counts_as_stopped(self):
        trips = summarize_device([fix(m, None) for m in range(0, 12)])
        self.assertEqual(trips.halt_count, 1)
        self.assertAlmostEqual(trips.halt_minutes, 11)


class TestEveryMinuteCounted(unittest.TestCase):
    """Running + halted + short stops + no data = the whole window."""

    def total(self, trips):
        return (
            trips.running_minutes
            + trips.halt_minutes
            + trips.short_stop_minutes
            + trips.no_data_minutes
        )

    def test_a_silent_night_parked_is_one_halt(self):
        # Parked at 20:00 (one stationary fix), nothing but heartbeats all
        # night, drives off from the same spot at 07:00.
        evening = T0.replace(hour=20)
        fixes = [
            ReportFix(12.97, 77.59, 30, evening - timedelta(minutes=2)),
            ReportFix(12.97, 77.59, 0, evening),
            ReportFix(12.9701, 77.59, 25, evening + timedelta(hours=11)),
            ReportFix(12.9711, 77.59, 30, evening + timedelta(hours=11, minutes=1)),
        ]
        trips = summarize_device(fixes)
        self.assertEqual(trips.halt_count, 1)
        self.assertAlmostEqual(trips.halt_minutes, 11 * 60)
        self.assertAlmostEqual(trips.running_minutes, 3)
        self.assertAlmostEqual(self.total(trips), 11 * 60 + 3)

    def test_reappearing_somewhere_else_after_a_silence_is_no_data(self):
        fixes = [fix(0, 0), fix(1, 0), fix(90, 0, lat=13.10)]  # ~14 km away, 89 min later
        trips = summarize_device(fixes)
        self.assertEqual(trips.halt_count, 0)
        self.assertAlmostEqual(trips.short_stop_minutes, 1)
        self.assertAlmostEqual(trips.no_data_minutes, 89)

    def test_a_stop_crossing_the_window_start_counts_only_inside_it(self):
        # Parked since 07:00; the window starts at 08:00 (T0); moves at 08:30.
        fixes = [fix(-60, 0), fix(30, 20, lat=12.9701), fix(31, 20, lat=12.9711)]
        trips = summarize_device(fixes, T0, T0 + timedelta(minutes=60))
        self.assertAlmostEqual(trips.halt_minutes, 30)
        self.assertEqual(trips.fix_count, 2)  # the 07:00 fix is context, not a position
        # 08:31 -> 09:00: the last fix was moving 29 minutes before the end.
        self.assertAlmostEqual(trips.running_minutes, 1)
        self.assertAlmostEqual(trips.no_data_minutes, 29)
        self.assertAlmostEqual(self.total(trips), 60)

    def test_time_before_the_first_fix_is_no_data(self):
        trips = summarize_device([fix(20, 30), fix(21, 30)], T0, T0 + timedelta(minutes=30))
        self.assertAlmostEqual(trips.no_data_minutes, 20)  # before 08:20
        # Still driving 9 minutes before the end: within the running gap.
        self.assertAlmostEqual(trips.running_minutes, 10)
        self.assertAlmostEqual(self.total(trips), 30)

    def test_still_parked_at_the_window_end_counts_to_the_end(self):
        trips = summarize_device([fix(0, 30), fix(1, 0)], T0, T0 + timedelta(minutes=45))
        self.assertAlmostEqual(trips.halt_minutes, 44)
        self.assertAlmostEqual(self.total(trips), 45)


class TestCleanRoute(unittest.TestCase):
    """The route the report measures -- the dashboard map's own filtering."""

    def at(self, seconds, lat, speed=40, lng=77.59, gps_fixed=True):
        return ReportFix(lat, lng, speed, T0 + timedelta(seconds=seconds), gps_fixed)

    def test_a_dirty_point_is_left_out_of_the_distance(self):
        # 40 km/h north; one fix lands ~250 m east of the road.
        route = [
            self.at(0, 12.970),
            self.at(10, 12.971),
            self.at(20, 12.972, lng=77.5923),
            self.at(30, 12.973),
            self.at(40, 12.974),
        ]
        self.assertEqual(len(clean_route(route)), 4)
        trips = summarize_device(route)
        self.assertAlmostEqual(trips.distance_km, 0.445, places=2)
        self.assertEqual(trips.fix_count, 5)

    def test_no_lock_and_unset_positions_are_left_out(self):
        route = [
            self.at(0, 12.970),
            self.at(10, 12.950, gps_fixed=False),
            self.at(20, 0.0, lng=0.0),
            self.at(30, 12.971),
        ]
        self.assertEqual([f.latitude for f in clean_route(route)], [12.970, 12.971])

    def test_real_driving_and_u_turns_stay(self):
        straight = [self.at(i * 10, 12.970 + i * 0.001) for i in range(5)]
        u_turn = [self.at(0, 12.97, 60), self.at(30, 12.9745, 60), self.at(60, 12.9702, 60)]
        self.assertEqual(len(clean_route(straight)), 5)
        self.assertEqual(len(clean_route(u_turn)), 3)

    def test_fixes_minutes_apart_are_not_judged(self):
        errand = [self.at(0, 12.97, 0), self.at(3600, 12.98, 0), self.at(7200, 12.97, 0)]
        self.assertEqual(len(clean_route(errand)), 3)

    def test_a_vehicle_with_only_unusable_fixes_reports_nothing_driven(self):
        trips = summarize_device(
            [self.at(0, 12.97, gps_fixed=False), self.at(10, 12.98, gps_fixed=False)]
        )
        self.assertEqual((trips.distance_km, trips.fix_count, trips.max_speed_kmh), (0, 2, None))


class TestFixTime(unittest.TestCase):
    def test_uses_the_trackers_clock(self):
        arrived = T0 + timedelta(minutes=40)
        self.assertEqual(fix_time(T0, arrived), T0)

    def test_falls_back_to_arrival_without_a_tracker_time(self):
        self.assertEqual(fix_time(None, T0), T0)

    def test_ignores_a_clock_that_is_plainly_wrong(self):
        # Ahead of arrival by more than 10 minutes, or a month behind it.
        self.assertEqual(fix_time(T0 + timedelta(minutes=11), T0), T0)
        self.assertEqual(fix_time(T0 - timedelta(days=31), T0), T0)
        # Small drift either way is still the tracker's own time.
        self.assertEqual(fix_time(T0 + timedelta(minutes=2), T0), T0 + timedelta(minutes=2))


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
            json={
                "username": "owner",
                "password": "owner1234",
                "role": "user",
                "devices": ["dev1"],
            },
            headers=self.admin,
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "owner", "password": "owner1234"}
        )
        self.owner = {"Authorization": f"Bearer {login.json()['token']}"}
        for device_id, lat, speed in (("dev1", 12.97, 30), ("dev1", 12.98, 45), ("dev2", 13.0, 60)):
            await self.client.post(
                f"{BASE}/ingest/location",
                json={
                    "device_id": device_id,
                    "latitude": lat,
                    "longitude": 77.59,
                    "speed_kmh": speed,
                },
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

    async def test_a_buffered_upload_is_timed_by_when_the_tracker_took_each_fix(self):
        # A tracker out of coverage: 30 minutes of driving, then a 15-minute
        # halt, all uploaded in one burst on reconnect -- by arrival time it
        # all happened within a second.
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        backlog = [(m, 40) for m in range(0, 31, 5)] + [(m, 0) for m in (31, 38, 46)]
        for i, (minute, speed) in enumerate(backlog):
            await self.client.post(
                f"{BASE}/ingest/location",
                json={
                    "device_id": "dev3",
                    "latitude": 12.9 + i * 0.01 if speed else 12.9 + 6 * 0.01,
                    "longitude": 77.6,
                    "speed_kmh": speed,
                    "fixed_at": (start + timedelta(minutes=minute)).isoformat(),
                },
                headers={"X-API-Key": API_KEY},
            )
        body = (await self.client.get(f"{BASE}/stats/report", headers=self.admin)).json()
        dev3 = next(d for d in body["devices"] if d["device_id"] == "dev3")
        # Running: every gap after a moving fix, 30 min of drive plus the
        # minute into the first stationary fix.
        self.assertAlmostEqual(dev3["running_minutes"], 31, places=3)
        self.assertEqual(dev3["halt_count"], 1)
        # Still parked there now: halted from minute 31 until now (~60).
        self.assertAlmostEqual(dev3["halt_minutes"], 29, places=1)
        total = sum(
            dev3[k]
            for k in ("running_minutes", "halt_minutes", "short_stop_minutes", "no_data_minutes")
        )
        self.assertAlmostEqual(total, 24 * 60, places=1)

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
