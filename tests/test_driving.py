"""Driving behaviour rules (api/driving.py) and the /stats/driving endpoint."""

import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from api.driving import ignition_on_spans, is_night, score_device
from api.reports import ReportFix

try:
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

# 14:00 IST: an ordinary afternoon, far from the night window.
T0 = datetime(2026, 9, 27, 8, 30, tzinfo=timezone.utc)
# 01:00 IST the same day.
NIGHT = datetime(2026, 9, 26, 19, 30, tzinfo=timezone.utc)
BASE = "/api/v1"
API_KEY = "test-secret"
ADMIN = {"username": "admin", "password": "adminpassword"}


def drive(start: datetime, speeds: list[int], step_s: int = 10) -> list[ReportFix]:
    """A fix every `step_s` seconds heading north at each speed in turn, the
    position advancing as far as that speed covers -- so the route is clean."""
    fixes, lat = [], 12.97
    for i, speed in enumerate(speeds):
        fixes.append(ReportFix(lat, 77.59, speed, start + timedelta(seconds=i * step_s)))
        lat += speed / 3.6 * step_s / 111_000
    return fixes


def score(fixes: list[ReportFix], **kwargs):
    return score_device(fixes, fixes[0].at, fixes[-1].at, **kwargs)


class TestScoreDevice(unittest.TestCase):
    def test_steady_daytime_driving_scores_full_marks(self):
        result = score(drive(T0, [50] * 61))
        self.assertEqual(result.score, 100)
        self.assertAlmostEqual(result.driving_minutes, 10.0)
        self.assertEqual(result.events, [])
        self.assertIsNone(result.idle_minutes)

    def test_too_little_driving_is_not_scored(self):
        self.assertIsNone(score(drive(T0, [50] * 7)).score)
        self.assertIsNone(score_device([], T0, T0 + timedelta(hours=1)).score)

    def test_overspeed_costs_points_and_is_reported_as_a_stretch(self):
        # Ten minutes, the middle three of them at 110.
        result = score(drive(T0, [60] * 21 + [110] * 18 + [60] * 22))
        self.assertAlmostEqual(result.overspeed_minutes, 3.0)
        self.assertLess(result.score, 100)
        [event] = result.events
        self.assertEqual((event.kind, event.max_speed_kmh), ("overspeed", 110))
        self.assertAlmostEqual(event.minutes, 3.0)

    def test_the_speed_limit_is_the_callers(self):
        fixes = drive(T0, [70] * 61)
        self.assertEqual(score(fixes).overspeed_minutes, 0)
        self.assertAlmostEqual(score(fixes, speed_limit_kmh=60).overspeed_minutes, 10.0)

    def test_a_junk_speed_reading_is_not_speeding(self):
        # GT06 junk arrives as 240-255; a fix with no lock has no real speed.
        fixes = drive(T0, [60] * 30 + [250] + [60] * 30)
        fixes[10] = ReportFix(fixes[10].latitude, 77.59, 150, fixes[10].at, gps_fixed=False)
        self.assertEqual(score(fixes).overspeed_minutes, 0)

    def test_a_brief_burst_over_the_limit_is_counted_but_not_an_event(self):
        result = score(drive(T0, [60] * 30 + [95] * 3 + [60] * 30))
        self.assertAlmostEqual(result.overspeed_minutes, 0.5)
        self.assertEqual(result.events, [])

    def test_sudden_stop_and_start_between_close_fixes(self):
        result = score(drive(T0, [60] * 30 + [10] + [10] * 5 + [55] + [55] * 30))
        self.assertEqual((result.sudden_stops, result.sudden_starts), (1, 1))
        self.assertLess(result.score, 100)

    def test_a_speed_drop_across_a_long_gap_is_not_sudden(self):
        # A minute between fixes: anything could have happened in between.
        result = score(drive(T0, [60, 60, 5, 5, 60, 60, 60, 60], step_s=60))
        self.assertEqual((result.sudden_stops, result.sudden_starts), (0, 0))

    def test_night_driving_is_counted_in_ist(self):
        self.assertTrue(is_night(NIGHT))
        self.assertFalse(is_night(T0))
        result = score(drive(NIGHT, [50] * 61))
        self.assertAlmostEqual(result.night_minutes, 10.0)
        self.assertEqual(result.score, 85)
        [event] = result.events
        self.assertEqual(event.kind, "night_drive")
        self.assertAlmostEqual(event.minutes, 10.0)

    def test_traffic_stops_do_not_split_one_night_drive(self):
        result = score(drive(NIGHT, [50] * 20 + [0] * 12 + [50] * 20))
        self.assertEqual([event.kind for event in result.events], ["night_drive"])

    def test_idle_is_engine_on_time_not_spent_driving(self):
        # Engine on for 30 minutes; the vehicle drives for the first 10.
        fixes = drive(T0, [50] * 61)
        parked = fixes[-1]
        fixes.append(ReportFix(parked.latitude, 77.59, 0, parked.at + timedelta(seconds=10)))
        fixes.append(ReportFix(parked.latitude, 77.59, 0, T0 + timedelta(minutes=30)))
        on = [(T0, T0 + timedelta(minutes=30))]
        result = score_device(fixes, T0, T0 + timedelta(minutes=30), ignition_on=on)
        self.assertAlmostEqual(result.idle_minutes, 20.0, places=0)
        self.assertEqual([event.kind for event in result.events], ["long_idle"])
        self.assertLess(result.score, 100)

    def test_no_ignition_history_is_unknown_not_zero(self):
        self.assertIsNone(score(drive(T0, [50] * 61)).idle_minutes)
        self.assertEqual(score(drive(T0, [50] * 61), ignition_on=[]).idle_minutes, 0)

    def test_a_day_parked_is_reported(self):
        fixes = [
            ReportFix(12.97, 77.59, 0, T0),
            ReportFix(12.97, 77.59, 0, T0 + timedelta(hours=30)),
        ]
        result = score_device(fixes, T0, T0 + timedelta(hours=30))
        [event] = result.events
        self.assertEqual(event.kind, "long_halt")
        self.assertAlmostEqual(event.minutes, 30 * 60)

    def test_a_silence_while_moving_is_not_driving_time(self):
        # Moving, then nothing for an hour: the tracker went quiet.
        fixes = drive(T0, [50] * 31)
        fixes.append(ReportFix(13.5, 77.59, 50, fixes[-1].at + timedelta(hours=1)))
        result = score_device(fixes, T0, fixes[-1].at)
        self.assertAlmostEqual(result.driving_minutes, 5.0)


class TestIgnitionSpans(unittest.TestCase):
    since = T0
    until = T0 + timedelta(hours=2)

    def spans(self, *flips):
        return ignition_on_spans(
            [(T0 + timedelta(minutes=m), state) for m, state in flips], self.since, self.until
        )

    def test_no_history_is_none(self):
        self.assertIsNone(self.spans())

    def test_on_then_off_inside_the_window(self):
        self.assertEqual(
            self.spans((10, True), (40, False)),
            [(T0 + timedelta(minutes=10), T0 + timedelta(minutes=40))],
        )

    def test_already_on_when_the_window_opens(self):
        self.assertEqual(
            self.spans((-90, True), (30, False)), [(T0, T0 + timedelta(minutes=30))]
        )

    def test_still_on_when_the_window_closes(self):
        self.assertEqual(self.spans((100, True)), [(T0 + timedelta(minutes=100), self.until)])

    def test_off_throughout_is_known_and_empty(self):
        self.assertEqual(self.spans((-90, False)), [])


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestDrivingEndpoint(unittest.IsolatedAsyncioTestCase):
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
        # Ten minutes each, ending now: dev1 at a steady 50, dev2 at 120.
        start = datetime.now(timezone.utc) - timedelta(minutes=10)
        for device_id, speed in (("dev1", 50), ("dev2", 120)):
            for fix in drive(start, [speed] * 61):
                await self.client.post(
                    f"{BASE}/ingest/location",
                    json={
                        "device_id": device_id,
                        "latitude": fix.latitude,
                        "longitude": fix.longitude,
                        "speed_kmh": fix.speed_kmh,
                        "fixed_at": fix.at.isoformat(),
                    },
                    headers={"X-API-Key": API_KEY},
                )

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def test_admin_gets_every_vehicle_worst_score_first(self):
        response = await self.client.get(f"{BASE}/stats/driving", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["speed_limit_kmh"], 80)
        self.assertEqual([d["device_id"] for d in body["devices"]], ["dev2", "dev1"])
        speeder, steady = body["devices"]
        self.assertLess(speeder["score"], steady["score"])
        self.assertEqual(speeder["events"][0]["kind"], "overspeed")
        self.assertIsNone(steady["idle_minutes"])

    async def test_a_user_sees_only_their_own_vehicles(self):
        body = (await self.client.get(f"{BASE}/stats/driving", headers=self.owner)).json()
        self.assertEqual([d["device_id"] for d in body["devices"]], ["dev1"])

    async def test_the_speed_limit_can_be_raised(self):
        body = (
            await self.client.get(f"{BASE}/stats/driving?speed_limit_kmh=130", headers=self.admin)
        ).json()
        self.assertEqual({d["overspeed_minutes"] for d in body["devices"]}, {0})

    async def test_needs_a_sign_in(self):
        self.assertEqual((await self.client.get(f"{BASE}/stats/driving")).status_code, 401)

    async def test_refuses_a_window_longer_than_eight_days(self):
        now = datetime.now(timezone.utc)
        window = urlencode(
            {"since": (now - timedelta(days=9)).isoformat(), "until": now.isoformat()}
        )
        response = await self.client.get(f"{BASE}/stats/driving?{window}", headers=self.admin)
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
