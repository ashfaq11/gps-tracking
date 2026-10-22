"""The alerts history: who sees which alerts, paging and filters, the read
mark, per-vehicle speed limits -- and that nothing can clear it. The alerts
themselves are written by database triggers (sql/schema.sql), which the
in-memory backend does not have, so rows are added here with `record`;
tests/test_alerts_postgres.py checks the triggers when TEST_PG_DSN is set."""

import unittest
from datetime import datetime, timedelta, timezone

try:
    from api.alerts_repository import AlertViewer
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

BASE = "/api/v1"
ADMIN = {"username": "admin", "password": "Admin-password1"}
T0 = datetime(2026, 10, 22, 4, 30, tzinfo=timezone.utc)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class AlertsTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(
            ApiConfig(
                backend="memory",
                bootstrap_admin_username=ADMIN["username"],
                bootstrap_admin_password=ADMIN["password"],
            )
        )
        self.client = AsgiClient(self.app)
        await self.client.__aenter__()
        self.admin = (await self.client.post(f"{BASE}/auth/login", json=ADMIN)).json()["token"]
        from api.state import ensure_alerts

        self.alerts = await ensure_alerts(self.app)

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    async def user(self, username, devices, geofences=False):
        created = await self.client.post(
            f"{BASE}/users",
            json={"username": username, "password": "Password-123", "role": "user",
                  "devices": devices, "geofence_enabled": geofences},
            headers=self.auth(self.admin),
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": username, "password": "Password-123"}
        )
        return login.json()["token"], created.json()["id"]

    async def get(self, path, token=None):
        return await self.client.get(f"{BASE}{path}", headers=self.auth(token or self.admin))


class TestVisibility(AlertsTestCase):
    async def test_an_account_sees_only_its_own_vehicles(self):
        self.alerts.record("dev-a", "stopped", T0)
        self.alerts.record("dev-b", "stopped", T0)
        token, _ = await self.user("asha", ["dev-a"])
        mine = (await self.get("/alerts", token)).json()
        self.assertEqual([e["device_id"] for e in mine["entries"]], ["dev-a"])
        everyone = (await self.get("/alerts")).json()
        self.assertEqual(everyone["total"], 2)

    async def test_a_geofence_alert_goes_to_whoever_drew_it(self):
        token, user_id = await self.user("asha", ["dev-a"], geofences=True)
        other, _ = await self.user("ravi", ["dev-a"], geofences=True)
        self.alerts.record("dev-a", "geofence_exit", T0, geofence_id=1, geofence_name="Depot", owner_id=user_id)
        self.assertEqual((await self.get("/alerts", token)).json()["total"], 1)
        self.assertEqual((await self.get("/alerts", other)).json()["total"], 0)
        self.assertEqual((await self.get("/alerts")).json()["total"], 1)  # an admin sees all

    async def test_geofence_alerts_stop_when_the_feature_is_switched_off(self):
        token, user_id = await self.user("asha", ["dev-a"], geofences=False)
        self.alerts.record("dev-a", "geofence_exit", T0, owner_id=user_id)
        self.assertEqual((await self.get("/alerts", token)).json()["total"], 0)

    async def test_a_vehicle_that_is_not_yours_is_a_404(self):
        token, _ = await self.user("asha", ["dev-a"])
        response = await self.get("/alerts?device_id=dev-b", token)
        self.assertEqual(response.status_code, 404)

    async def test_it_needs_a_sign_in(self):
        response = await self.client.get(f"{BASE}/alerts")
        self.assertEqual(response.status_code, 401)


class TestReading(AlertsTestCase):
    async def test_newest_first_and_paged(self):
        for n in range(5):
            self.alerts.record("dev-a", "moving", T0 + timedelta(minutes=n))
        first = (await self.get("/alerts?size=2")).json()
        self.assertEqual((first["total"], first["pages"], len(first["entries"])), (5, 3, 2))
        self.assertEqual(first["entries"][0]["occurred_at"], "2026-10-22T04:34:00Z")
        last = (await self.get("/alerts?size=2&page=3")).json()
        self.assertEqual(len(last["entries"]), 1)

    async def test_filters_by_day_kind_and_vehicle(self):
        self.alerts.record("dev-a", "stopped", T0)
        self.alerts.record("dev-a", "overspeed", T0 + timedelta(hours=1), speed_kmh=95, limit_kmh=80)
        self.alerts.record("dev-b", "overspeed", T0 + timedelta(days=1), speed_kmh=99, limit_kmh=80)
        day = (await self.get("/alerts?since=2026-10-22T00:00:00Z&until=2026-10-22T23:59:59Z")).json()
        self.assertEqual(day["total"], 2)
        kind = (await self.get("/alerts?kind=overspeed&kind=harsh_braking")).json()
        self.assertEqual(kind["total"], 2)
        device = (await self.get("/alerts?kind=overspeed&device_id=dev-a")).json()
        self.assertEqual(device["entries"][0]["limit_kmh"], 80)
        self.assertEqual(device["total"], 1)

    async def test_after_id_returns_only_newer_alerts(self):
        first = self.alerts.record("dev-a", "moving", T0)
        self.alerts.record("dev-a", "stopped", T0 + timedelta(minutes=1))
        page = (await self.get(f"/alerts?after_id={first}")).json()
        self.assertEqual([e["kind"] for e in page["entries"]], ["stopped"])

    async def test_times_are_utc(self):
        self.alerts.record("dev-a", "moving", datetime(2026, 10, 22, 10, 0))  # naive: UTC
        entry = (await self.get("/alerts")).json()["entries"][0]
        self.assertTrue(entry["occurred_at"].endswith("Z"))


class TestReadMark(AlertsTestCase):
    async def test_unread_counts_from_the_mark_and_only_moves_forward(self):
        a = self.alerts.record("dev-a", "moving", T0)
        b = self.alerts.record("dev-a", "stopped", T0 + timedelta(minutes=1))
        self.assertEqual((await self.get("/alerts/unread")).json(), {"unread": 2, "seen_id": 0})
        marked = await self.client.put(f"{BASE}/alerts/seen", json={"seen_id": a}, headers=self.auth(self.admin))
        self.assertEqual(marked.json(), {"unread": 1, "seen_id": a})
        back = await self.client.put(f"{BASE}/alerts/seen", json={"seen_id": 0}, headers=self.auth(self.admin))
        self.assertEqual(back.json()["seen_id"], a)  # never back
        done = await self.client.put(f"{BASE}/alerts/seen", json={"seen_id": b}, headers=self.auth(self.admin))
        self.assertEqual(done.json(), {"unread": 0, "seen_id": b})

    async def test_each_account_has_its_own_mark(self):
        self.alerts.record("dev-a", "moving", T0)
        token, _ = await self.user("asha", ["dev-a"])
        await self.client.put(f"{BASE}/alerts/seen", json={"seen_id": 1}, headers=self.auth(token))
        self.assertEqual((await self.get("/alerts/unread", token)).json()["unread"], 0)
        self.assertEqual((await self.get("/alerts/unread")).json()["unread"], 1)


class TestNothingClearsIt(AlertsTestCase):
    async def test_there_is_no_delete(self):
        self.alerts.record("dev-a", "moving", T0)
        for path in ("/alerts", "/alerts/1", "/alerts/seen"):
            response = await self.client.delete(f"{BASE}{path}", headers=self.auth(self.admin))
            self.assertIn(response.status_code, (404, 405), path)
        self.assertEqual((await self.get("/alerts")).json()["total"], 1)


class TestSpeedLimits(AlertsTestCase):
    async def test_set_and_read_back_for_your_own_vehicle(self):
        token, _ = await self.user("asha", ["dev-a"])
        put = await self.client.put(
            f"{BASE}/alerts/speed-limits/dev-a", json={"limit_kmh": 60}, headers=self.auth(token)
        )
        self.assertEqual(put.json(), {"device_id": "dev-a", "limit_kmh": 60})
        self.assertEqual((await self.get("/alerts/speed-limits", token)).json(), [put.json()])

    async def test_not_yours_is_a_404_and_the_range_is_checked(self):
        token, _ = await self.user("asha", ["dev-a"])
        other = await self.client.put(
            f"{BASE}/alerts/speed-limits/dev-b", json={"limit_kmh": 60}, headers=self.auth(token)
        )
        self.assertEqual(other.status_code, 404)
        silly = await self.client.put(
            f"{BASE}/alerts/speed-limits/dev-a", json={"limit_kmh": 5}, headers=self.auth(token)
        )
        self.assertEqual(silly.status_code, 422)

    async def test_an_account_lists_only_its_vehicles_limits(self):
        await self.client.put(f"{BASE}/alerts/speed-limits/dev-b", json={"limit_kmh": 50}, headers=self.auth(self.admin))
        token, _ = await self.user("asha", ["dev-a"])
        self.assertEqual((await self.get("/alerts/speed-limits", token)).json(), [])


class TestViewerRule(unittest.TestCase):
    @unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
    def test_sees(self):
        viewer = AlertViewer(user_id=3, is_admin=False, devices=frozenset({"a"}), can_use_geofences=True)
        self.assertTrue(viewer.sees("a", "stopped", None))
        self.assertFalse(viewer.sees("b", "stopped", None))
        self.assertTrue(viewer.sees("a", "geofence_exit", 3))
        self.assertFalse(viewer.sees("a", "geofence_exit", 4))


if __name__ == "__main__":
    unittest.main()
