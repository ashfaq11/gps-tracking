"""Tracker allowlist admin API: /trackers/allowlist and /trackers/attempts."""

import unittest

try:
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

BASE = "/api/v1"
ADMIN = {"username": "admin", "password": "adminpassword"}
IMEI = "868120303372449"


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestTrackers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(
            ApiConfig(
                backend="memory",
                ingest_api_key="k",
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

    async def trackers_repo(self):
        from api.state import ensure_trackers

        return await ensure_trackers(self.app)

    async def test_approving_a_refused_tracker_moves_it_to_the_allowlist(self):
        repo = await self.trackers_repo()
        repo.record_attempt(IMEI, "10.1.2.3:40000")
        repo.record_attempt(IMEI, "10.1.2.3:40001")
        attempts = (await self.client.get(f"{BASE}/trackers/attempts", headers=self.admin)).json()
        self.assertEqual(
            [(a["device_id"], a["attempts"], a["last_peer"]) for a in attempts],
            [(IMEI, 2, "10.1.2.3:40001")],
        )

        added = await self.client.post(
            f"{BASE}/trackers/allowlist", json={"device_id": IMEI}, headers=self.admin
        )
        self.assertEqual(added.status_code, 201)
        self.assertEqual((added.json()["source"], added.json()["added_by"]), ("admin", "admin"))
        self.assertEqual(
            (await self.client.get(f"{BASE}/trackers/attempts", headers=self.admin)).json(), []
        )
        listed = (await self.client.get(f"{BASE}/trackers/allowlist", headers=self.admin)).json()
        self.assertEqual([t["device_id"] for t in listed], [IMEI])

    async def test_allowing_twice_is_harmless(self):
        for _ in range(2):
            response = await self.client.post(
                f"{BASE}/trackers/allowlist", json={"device_id": IMEI}, headers=self.admin
            )
            self.assertEqual(response.status_code, 201)
        listed = (await self.client.get(f"{BASE}/trackers/allowlist", headers=self.admin)).json()
        self.assertEqual(len(listed), 1)

    async def test_removing(self):
        await self.client.post(
            f"{BASE}/trackers/allowlist", json={"device_id": IMEI}, headers=self.admin
        )
        gone = await self.client.delete(f"{BASE}/trackers/allowlist/{IMEI}", headers=self.admin)
        self.assertEqual(gone.status_code, 204)
        again = await self.client.delete(f"{BASE}/trackers/allowlist/{IMEI}", headers=self.admin)
        self.assertEqual(again.status_code, 404)

    async def test_dismissing_an_attempt(self):
        repo = await self.trackers_repo()
        repo.record_attempt(IMEI)
        path = f"{BASE}/trackers/attempts/{IMEI}"
        self.assertEqual((await self.client.delete(path, headers=self.admin)).status_code, 204)
        self.assertEqual((await self.client.delete(path, headers=self.admin)).status_code, 404)

    async def test_only_digits_are_accepted(self):
        for bad in ("abc", "12", "8681203033724491234567", "86812030337244'--"):
            response = await self.client.post(
                f"{BASE}/trackers/allowlist", json={"device_id": bad}, headers=self.admin
            )
            self.assertEqual(response.status_code, 422, bad)

    async def test_admin_only(self):
        await self.client.post(
            f"{BASE}/users",
            json={"username": "owner", "password": "owner1234", "role": "user", "devices": []},
            headers=self.admin,
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "owner", "password": "owner1234"}
        )
        owner = {"Authorization": f"Bearer {login.json()['token']}"}
        for method, path in (
            ("get", "/trackers/allowlist"),
            ("get", "/trackers/attempts"),
            ("delete", f"/trackers/allowlist/{IMEI}"),
        ):
            response = await getattr(self.client, method)(f"{BASE}{path}", headers=owner)
            self.assertEqual(response.status_code, 403, path)
        self.assertEqual((await self.client.get(f"{BASE}/trackers/allowlist")).status_code, 401)

    async def test_hold_is_the_default_and_refuses_a_new_tracker(self):
        settings = await self.client.get(f"{BASE}/trackers/settings", headers=self.admin)
        self.assertEqual(settings.status_code, 200)
        self.assertFalse(settings.json()["auto_approve"])
        repo = await self.trackers_repo()
        self.assertFalse(repo.gateway_admit(IMEI, "10.0.0.1:1"))
        attempts = (await self.client.get(f"{BASE}/trackers/attempts", headers=self.admin)).json()
        self.assertEqual([a["device_id"] for a in attempts], [IMEI])

    async def test_auto_approve_admits_any_new_tracker_and_lists_it_as_auto(self):
        repo = await self.trackers_repo()
        repo.gateway_admit(IMEI, "10.0.0.1:1")  # refused while on hold
        changed = await self.client.put(
            f"{BASE}/trackers/settings", json={"auto_approve": True}, headers=self.admin
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            (changed.json()["auto_approve"], changed.json()["changed_by"]), (True, "admin")
        )

        self.assertTrue(repo.gateway_admit(IMEI, "10.0.0.1:2"))  # the one that was waiting
        self.assertTrue(repo.gateway_admit("868120303372450", "10.0.0.2:1"))  # a brand-new one
        allowed = (await self.client.get(f"{BASE}/trackers/allowlist", headers=self.admin)).json()
        self.assertEqual(
            sorted((t["device_id"], t["source"]) for t in allowed),
            [(IMEI, "auto"), ("868120303372450", "auto")],
        )
        self.assertEqual(
            (await self.client.get(f"{BASE}/trackers/attempts", headers=self.admin)).json(), []
        )

    async def test_switching_back_to_hold_keeps_trackers_already_auto_approved(self):
        repo = await self.trackers_repo()
        await self.client.put(
            f"{BASE}/trackers/settings", json={"auto_approve": True}, headers=self.admin
        )
        repo.gateway_admit(IMEI, "p")
        await self.client.put(
            f"{BASE}/trackers/settings", json={"auto_approve": False}, headers=self.admin
        )
        self.assertTrue(repo.gateway_admit(IMEI, "p"))
        self.assertFalse(repo.gateway_admit("868120303372450", "p"))

    async def test_only_an_admin_may_change_the_setting(self):
        await self.client.post(
            f"{BASE}/users",
            json={"username": "owner", "password": "owner1234", "role": "user", "devices": []},
            headers=self.admin,
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "owner", "password": "owner1234"}
        )
        owner = {"Authorization": f"Bearer {login.json()['token']}"}
        put = await self.client.put(
            f"{BASE}/trackers/settings", json={"auto_approve": True}, headers=owner
        )
        self.assertEqual(put.status_code, 403)
        self.assertEqual(
            (await self.client.get(f"{BASE}/trackers/settings", headers=owner)).status_code, 403
        )


if __name__ == "__main__":
    unittest.main()
