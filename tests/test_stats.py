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
FIX = {"device_id": "dev1", "latitude": 12.97, "longitude": 77.59}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestStats(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsgiClient(
            create_app(
                ApiConfig(
                    backend="memory",
                    ingest_api_key=API_KEY,
                    bootstrap_admin_username=ADMIN["username"],
                    bootstrap_admin_password=ADMIN["password"],
                )
            )
        )
        await self.client.__aenter__()
        login = await self.client.post(f"{BASE}/auth/login", json=ADMIN)
        self.token = login.json()["token"]

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def ingest(self, **overrides):
        return await self.client.post(
            f"{BASE}/ingest/location", json={**FIX, **overrides}, headers={"X-API-Key": API_KEY}
        )

    async def get(self, path):
        return await self.client.get(path, headers=self.auth())

    async def test_summary_is_all_zeros_before_any_fix(self):
        body = (await self.get(f"{BASE}/stats/summary")).json()
        self.assertEqual(body["devices_total"], 0)
        self.assertEqual(body["fixes_total"], 0)
        self.assertIsNone(body["last_fix_at"])

    async def test_summary_counts_devices_and_fixes(self):
        await self.ingest()
        await self.ingest()
        await self.ingest(device_id="dev2")
        body = (await self.get(f"{BASE}/stats/summary")).json()
        self.assertEqual(body["devices_total"], 2)
        self.assertEqual(body["fixes_total"], 3)

    async def test_freshly_ingested_devices_count_as_active(self):
        await self.ingest()
        body = (await self.get(f"{BASE}/stats/summary")).json()
        self.assertEqual(body["devices_active"], 1)

    async def test_summary_echoes_the_windows_it_used(self):
        response = await self.get(f"{BASE}/stats/summary?active_minutes=5&recent_hours=6")
        body = response.json()
        self.assertEqual(body["active_window_minutes"], 5)
        self.assertEqual(body["recent_window_hours"], 6)

    async def test_summary_reports_the_latest_fix_time(self):
        await self.ingest()
        body = (await self.get(f"{BASE}/stats/summary")).json()
        self.assertIsNotNone(body["last_fix_at"])

    async def test_summary_rejects_an_out_of_range_window(self):
        response = await self.get(f"{BASE}/stats/summary?active_minutes=0")
        self.assertEqual(response.status_code, 422)

    async def test_summary_requires_sign_in(self):
        response = await self.client.get(f"{BASE}/stats/summary")
        self.assertEqual(response.status_code, 401)

    async def test_fixes_over_time_is_empty_with_no_data(self):
        response = await self.get(f"{BASE}/stats/fixes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_fixes_over_time_buckets_and_counts(self):
        for _ in range(3):
            await self.ingest()
        buckets = (await self.get(f"{BASE}/stats/fixes?hours=1")).json()
        self.assertEqual(sum(b["fixes"] for b in buckets), 3)

    async def test_fixes_over_time_is_in_ascending_time_order(self):
        for _ in range(5):
            await self.ingest()
        buckets = (await self.get(f"{BASE}/stats/fixes?bucket_minutes=1")).json()
        times = [b["bucket"] for b in buckets]
        self.assertEqual(times, sorted(times))

    async def test_fixes_over_time_rejects_a_bad_bucket_width(self):
        response = await self.get(f"{BASE}/stats/fixes?bucket_minutes=0")
        self.assertEqual(response.status_code, 422)

    async def test_fixes_over_time_requires_sign_in(self):
        response = await self.client.get(f"{BASE}/stats/fixes")
        self.assertEqual(response.status_code, 401)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestStatsScoping(unittest.IsolatedAsyncioTestCase):
    """A non-admin's numbers must cover only their own devices."""

    async def asyncSetUp(self):
        self.client = AsgiClient(
            create_app(
                ApiConfig(
                    backend="memory",
                    ingest_api_key=API_KEY,
                    bootstrap_admin_username=ADMIN["username"],
                    bootstrap_admin_password=ADMIN["password"],
                )
            )
        )
        await self.client.__aenter__()
        login = await self.client.post(f"{BASE}/auth/login", json=ADMIN)
        admin_token = login.json()["token"]

        await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": ["dev1"],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        user_login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
        )
        self.user_token = user_login.json()["token"]

        for device_id in ("dev1", "dev2"):
            await self.client.post(
                f"{BASE}/ingest/location",
                json={**FIX, "device_id": device_id},
                headers={"X-API-Key": API_KEY},
            )

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    def auth(self):
        return {"Authorization": f"Bearer {self.user_token}"}

    async def test_summary_counts_only_assigned_devices(self):
        body = (await self.client.get(f"{BASE}/stats/summary", headers=self.auth())).json()
        self.assertEqual(body["devices_total"], 1)
        self.assertEqual(body["fixes_total"], 1)

    async def test_fixes_over_time_counts_only_assigned_devices(self):
        buckets = (await self.client.get(f"{BASE}/stats/fixes?hours=1", headers=self.auth())).json()
        self.assertEqual(sum(b["fixes"] for b in buckets), 1)


if __name__ == "__main__":
    unittest.main()
