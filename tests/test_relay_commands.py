"""Engine cut-off: POST /devices/{id}/relay and GET /devices/{id}/commands."""

import unittest
from datetime import datetime, timedelta, timezone

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
class TestRelayCommands(unittest.IsolatedAsyncioTestCase):
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

    async def report(self, device_id: str, ignition: bool | None):
        body = {"device_id": device_id, "latitude": 12.97, "longitude": 77.59, "speed_kmh": 0}
        if ignition is not None:
            body["ignition"] = ignition
        response = await self.client.post(
            f"{BASE}/ingest/location", json=body, headers={"X-API-Key": API_KEY}
        )
        self.assertEqual(response.status_code, 202)

    async def relay(self, device_id: str, action: str, headers=None):
        return await self.client.post(
            f"{BASE}/devices/{device_id}/relay",
            json={"action": action},
            headers=self.admin if headers is None else headers,
        )

    async def test_cut_is_queued_while_the_ignition_is_off(self):
        await self.report("dev1", ignition=False)
        response = await self.relay("dev1", "cut")
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(
            (body["action"], body["command"], body["status"], body["requested_by"]),
            ("cut", "RELAY,1#", "queued", "admin"),
        )

    async def test_cut_is_refused_while_the_ignition_is_on(self):
        await self.report("dev1", ignition=True)
        response = await self.relay("dev1", "cut")
        self.assertEqual(response.status_code, 409)
        self.assertIn("ignition is off", response.json()["detail"])
        listed = await self.client.get(f"{BASE}/devices/dev1/commands", headers=self.admin)
        self.assertEqual(listed.json(), [])

    async def test_cut_is_refused_when_the_ignition_was_never_reported(self):
        await self.report("dev1", ignition=None)
        response = await self.relay("dev1", "cut")
        self.assertEqual(response.status_code, 409)
        self.assertIn("not reported its ignition", response.json()["detail"])

    async def test_restore_is_accepted_whatever_the_ignition(self):
        await self.report("dev1", ignition=True)
        response = await self.relay("dev1", "restore")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["command"], "RELAY,0#")

    async def test_only_an_admin_may_send_commands(self):
        await self.report("dev1", ignition=False)
        await self.client.post(
            f"{BASE}/users",
            json={"username": "owner", "password": "owner1234", "role": "user", "devices": ["dev1"]},
            headers=self.admin,
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "owner", "password": "owner1234"}
        )
        owner = {"Authorization": f"Bearer {login.json()['token']}"}
        self.assertEqual((await self.relay("dev1", "cut", headers=owner)).status_code, 403)
        listed = await self.client.get(f"{BASE}/devices/dev1/commands", headers=owner)
        self.assertEqual(listed.status_code, 403)
        self.assertEqual((await self.relay("dev1", "cut", headers={})).status_code, 401)

    async def test_a_newer_command_supersedes_one_still_queued(self):
        await self.report("dev1", ignition=False)
        await self.relay("dev1", "cut")
        await self.relay("dev1", "restore")
        listed = (await self.client.get(f"{BASE}/devices/dev1/commands", headers=self.admin)).json()
        self.assertEqual(
            [(c["action"], c["status"]) for c in listed],
            [("restore", "queued"), ("cut", "superseded")],
        )

    async def test_an_undelivered_command_reads_as_expired(self):
        await self.report("dev1", ignition=False)
        command = (await self.relay("dev1", "cut")).json()
        self.app.state.commands.set_status(
            command["id"], expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        listed = (await self.client.get(f"{BASE}/devices/dev1/commands", headers=self.admin)).json()
        self.assertEqual(listed[0]["status"], "expired")

    async def test_history_is_per_device_and_newest_first(self):
        await self.report("dev1", ignition=False)
        await self.report("dev2", ignition=False)
        await self.relay("dev1", "restore")
        await self.relay("dev2", "cut")
        await self.relay("dev1", "cut")
        listed = (
            await self.client.get(f"{BASE}/devices/dev1/commands?limit=5", headers=self.admin)
        ).json()
        self.assertEqual([c["action"] for c in listed], ["cut", "restore"])
        self.assertTrue(all(c["device_id"] == "dev1" for c in listed))

    async def test_unknown_action_is_rejected(self):
        response = await self.relay("dev1", "explode")
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
