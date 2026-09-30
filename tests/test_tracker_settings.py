"""Tracker settings: POST /devices/{id}/tracker/interval and /tracker/params."""

import unittest

try:
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

BASE = "/api/v1"
API_KEY = "test-secret"
ADMIN = {"username": "admin", "password": "adminpassword"}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestTrackerSettings(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def interval(self, moving_s, parked_s, headers=None):
        return await self.client.post(
            f"{BASE}/devices/dev1/tracker/interval",
            json={"moving_s": moving_s, "parked_s": parked_s},
            headers=self.admin if headers is None else headers,
        )

    async def state(self):
        response = await self.client.get(f"{BASE}/devices/dev1/tracker", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        return response.json()["commands"]

    async def test_interval_queues_the_manuals_timer_command(self):
        response = await self.interval(10, 3600)
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(
            (body["action"], body["command"], body["status"], body["requested_by"]),
            ("timer", "TIMER,10,3600#", "queued", "admin"),
        )

    async def test_interval_outside_the_manuals_limits_is_rejected(self):
        for moving_s, parked_s in [(4, 300), (18001, 300), (10, 299), (10, 18001)]:
            self.assertEqual((await self.interval(moving_s, parked_s)).status_code, 422)
        self.assertEqual(await self.state(), [])

    async def test_params_queues_param_and_shows_in_the_tracker_state(self):
        response = await self.client.post(
            f"{BASE}/devices/dev1/tracker/params", headers=self.admin
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["command"], "PARAM#")
        self.assertEqual([c["command"] for c in await self.state()], ["PARAM#"])

    async def test_settings_and_engine_commands_do_not_supersede_each_other(self):
        await self.client.post(
            f"{BASE}/devices/dev1/relay", json={"action": "restore"}, headers=self.admin
        )
        await self.interval(10, 3600)
        await self.interval(15, 3600)
        commands = (
            await self.client.get(f"{BASE}/devices/dev1/commands", headers=self.admin)
        ).json()
        self.assertEqual(
            [(c["command"], c["status"]) for c in commands],
            [("TIMER,15,3600#", "queued"), ("TIMER,10,3600#", "superseded"), ("RELAY,0#", "queued")],
        )
        # Each panel lists only its own kind.
        relay = (await self.client.get(f"{BASE}/devices/dev1/relay", headers=self.admin)).json()
        self.assertEqual([c["command"] for c in relay["commands"]], ["RELAY,0#"])
        self.assertEqual(
            [c["command"] for c in await self.state()], ["TIMER,15,3600#", "TIMER,10,3600#"]
        )

    async def test_reading_settings_does_not_cancel_a_queued_interval(self):
        await self.interval(10, 3600)
        await self.client.post(f"{BASE}/devices/dev1/tracker/params", headers=self.admin)
        self.assertEqual(
            [(c["command"], c["status"]) for c in await self.state()],
            [("PARAM#", "queued"), ("TIMER,10,3600#", "queued")],
        )

    async def test_only_an_admin_may_change_settings(self):
        await self.client.post(
            f"{BASE}/users",
            json={"username": "owner", "password": "owner1234", "role": "user", "devices": ["dev1"]},
            headers=self.admin,
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "owner", "password": "owner1234"}
        )
        owner = {"Authorization": f"Bearer {login.json()['token']}"}
        self.assertEqual((await self.interval(10, 3600, headers=owner)).status_code, 403)
        params = await self.client.post(f"{BASE}/devices/dev1/tracker/params", headers=owner)
        self.assertEqual(params.status_code, 403)
        listed = await self.client.get(f"{BASE}/devices/dev1/tracker", headers=owner)
        self.assertEqual(listed.status_code, 403)
        self.assertEqual((await self.interval(10, 3600, headers={})).status_code, 401)


if __name__ == "__main__":
    unittest.main()
