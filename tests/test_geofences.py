"""Geofences: drawing, editing and deleting them, who can see which, the
crossings report, and the alert message. Crossing *detection* is the
`check_geofences` trigger in sql/schema.sql, which the in-memory backend does
not have -- report rows are added here with `record_event` instead."""

import unittest

try:
    from api.config import ApiConfig
    from api.main import create_app
    from api.push import geofence_message

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

BASE = "/api/v1"
ADMIN = {"username": "admin", "password": "adminpassword"}

CIRCLE = {
    "name": "Depot",
    "kind": "circle",
    "center": {"lat": 16.3067, "lng": 80.4365},
    "radius_m": 500,
    "device_ids": ["dev-a"],
}
SQUARE = {
    "name": "Yard",
    "kind": "polygon",
    "vertices": [
        {"lat": 16.295, "lng": 80.445},
        {"lat": 16.295, "lng": 80.455},
        {"lat": 16.305, "lng": 80.455},
        {"lat": 16.305, "lng": 80.445},
    ],
    "device_ids": ["dev-a"],
}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class GeofenceTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    async def user_token(self, username: str, devices: list[str]) -> str:
        await self.client.post(
            f"{BASE}/users",
            json={"username": username, "password": "password123", "role": "user", "devices": devices},
            headers=self.auth(self.admin),
        )
        response = await self.client.post(
            f"{BASE}/auth/login", json={"username": username, "password": "password123"}
        )
        return response.json()["token"]

    async def create(self, body, token=None):
        return await self.client.post(
            f"{BASE}/geofences", json=body, headers=self.auth(token or self.admin)
        )


class TestDrawing(GeofenceTestCase):
    async def test_a_circle_round_trips(self):
        response = await self.create(CIRCLE)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["kind"], "circle")
        self.assertEqual(body["radius_m"], 500)
        self.assertEqual(body["center"], {"lat": 16.3067, "lng": 80.4365})
        self.assertEqual(body["device_ids"], ["dev-a"])
        self.assertTrue(body["alert_on_exit"])
        self.assertFalse(body["alert_on_enter"])

    async def test_a_polygon_keeps_its_points_in_order(self):
        body = (await self.create(SQUARE)).json()
        self.assertEqual(body["vertices"], SQUARE["vertices"])

    async def test_a_polygon_needs_three_points(self):
        response = await self.create({**SQUARE, "vertices": SQUARE["vertices"][:2]})
        self.assertEqual(response.status_code, 422)

    async def test_a_circle_needs_a_centre_and_radius(self):
        response = await self.create({"name": "x", "kind": "circle", "radius_m": 100})
        self.assertEqual(response.status_code, 422)

    async def test_a_blank_name_is_refused(self):
        response = await self.create({**CIRCLE, "name": "   "})
        self.assertEqual(response.status_code, 422)

    async def test_editing_changes_only_what_is_sent(self):
        fence = (await self.create(CIRCLE)).json()
        response = await self.client.patch(
            f"{BASE}/geofences/{fence['id']}",
            json={"radius_m": 800, "alert_on_enter": True},
            headers=self.auth(self.admin),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["radius_m"], 800)
        self.assertEqual(body["center"], CIRCLE["center"])
        self.assertEqual(body["name"], "Depot")
        self.assertTrue(body["alert_on_enter"])

    async def test_switching_shape_needs_the_new_shapes_fields(self):
        fence = (await self.create(CIRCLE)).json()
        response = await self.client.patch(
            f"{BASE}/geofences/{fence['id']}",
            json={"kind": "polygon"},
            headers=self.auth(self.admin),
        )
        self.assertEqual(response.status_code, 422)

    async def test_deleting_removes_it(self):
        fence = (await self.create(CIRCLE)).json()
        response = await self.client.delete(
            f"{BASE}/geofences/{fence['id']}", headers=self.auth(self.admin)
        )
        self.assertEqual(response.status_code, 204)
        response = await self.client.get(
            f"{BASE}/geofences/{fence['id']}", headers=self.auth(self.admin)
        )
        self.assertEqual(response.status_code, 404)


class TestWhoSeesWhat(GeofenceTestCase):
    async def test_signing_in_is_required(self):
        response = await self.client.get(f"{BASE}/geofences")
        self.assertEqual(response.status_code, 401)

    async def test_a_user_can_only_watch_their_own_vehicles(self):
        token = await self.user_token("owner", ["dev-a"])
        self.assertEqual((await self.create(CIRCLE, token)).status_code, 201)
        response = await self.create({**CIRCLE, "device_ids": ["dev-b"]}, token)
        self.assertEqual(response.status_code, 422)

    async def test_a_user_sees_only_their_own_geofences(self):
        await self.create({**CIRCLE, "name": "Admin's"})
        token = await self.user_token("owner", ["dev-a"])
        mine = (await self.create(CIRCLE, token)).json()

        listed = (await self.client.get(f"{BASE}/geofences", headers=self.auth(token))).json()
        self.assertEqual([f["id"] for f in listed], [mine["id"]])

        everything = (await self.client.get(f"{BASE}/geofences", headers=self.auth(self.admin))).json()
        self.assertEqual(len(everything), 2)

    async def test_someone_elses_geofence_reads_as_missing(self):
        theirs = (await self.create(CIRCLE)).json()
        token = await self.user_token("owner", ["dev-a"])
        for response in (
            await self.client.get(f"{BASE}/geofences/{theirs['id']}", headers=self.auth(token)),
            await self.client.patch(
                f"{BASE}/geofences/{theirs['id']}", json={"name": "x"}, headers=self.auth(token)
            ),
            await self.client.delete(f"{BASE}/geofences/{theirs['id']}", headers=self.auth(token)),
        ):
            self.assertEqual(response.status_code, 404)


class TestReport(GeofenceTestCase):
    async def test_crossings_are_listed_newest_first_with_whether_they_alerted(self):
        fence = (await self.create(CIRCLE)).json()
        repo = self.app.state.geofences
        repo.record_event(fence["id"], "dev-a", "exit")
        repo.record_event(fence["id"], "dev-a", "enter")

        events = (
            await self.client.get(f"{BASE}/geofences/events", headers=self.auth(self.admin))
        ).json()
        self.assertEqual([e["kind"] for e in events], ["enter", "exit"])
        self.assertEqual([e["alerted"] for e in events], [False, True])
        self.assertEqual(events[0]["geofence_name"], "Depot")

    async def test_after_id_returns_only_newer_crossings(self):
        fence = (await self.create(CIRCLE)).json()
        repo = self.app.state.geofences
        first = repo.record_event(fence["id"], "dev-a", "exit")
        repo.record_event(fence["id"], "dev-a", "enter")

        events = (
            await self.client.get(
                f"{BASE}/geofences/events?after_id={first}", headers=self.auth(self.admin)
            )
        ).json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "enter")

    async def test_a_user_sees_only_their_own_geofences_crossings(self):
        admins = (await self.create(CIRCLE)).json()
        token = await self.user_token("owner", ["dev-a"])
        mine = (await self.create({**CIRCLE, "name": "Mine"}, token)).json()
        repo = self.app.state.geofences
        repo.record_event(admins["id"], "dev-a", "exit")
        repo.record_event(mine["id"], "dev-a", "exit")

        events = (
            await self.client.get(f"{BASE}/geofences/events", headers=self.auth(token))
        ).json()
        self.assertEqual([e["geofence_name"] for e in events], ["Mine"])

    async def test_deleting_a_geofence_removes_its_crossings(self):
        fence = (await self.create(CIRCLE)).json()
        self.app.state.geofences.record_event(fence["id"], "dev-a", "exit")
        await self.client.delete(f"{BASE}/geofences/{fence['id']}", headers=self.auth(self.admin))
        events = (
            await self.client.get(f"{BASE}/geofences/events", headers=self.auth(self.admin))
        ).json()
        self.assertEqual(events, [])


class TestAlertMessage(unittest.TestCase):
    def test_names_the_vehicle_and_the_geofence(self):
        message = geofence_message(
            {"id": 7, "kind": "exit", "device_id": "dev-a", "device_name": "SP",
             "geofence_id": 3, "geofence_name": "Depot"}
        )
        self.assertEqual(message["title"], "SP left Depot")
        self.assertEqual(message["tag"], "geofence-7")

    def test_falls_back_to_the_device_id_without_a_name(self):
        message = geofence_message(
            {"id": 8, "kind": "enter", "device_id": "dev-a", "device_name": None,
             "geofence_name": "Depot"}
        )
        self.assertEqual(message["title"], "dev-a entered Depot")


if __name__ == "__main__":
    unittest.main()
