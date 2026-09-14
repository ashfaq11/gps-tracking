"""
The API must work on hosts that never run ASGI lifespan events.

Vercel's Python runtime is the case that matters here: if the repository were
only built in `lifespan`, every request would answer 503 in production while
passing locally under uvicorn.
"""

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
class TestWithoutLifespan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Deliberately NOT entering the client context, so no startup event
        # ever fires -- exactly how the app is invoked on Vercel.
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

    async def auth(self):
        # Login is itself a plain request, so it exercises the same lazy
        # cold-start path as everything else in this file -- no lifespan
        # needed here either.
        response = await self.client.post(f"{BASE}/auth/login", json=ADMIN)
        return {"Authorization": f"Bearer {response.json()['token']}"}

    async def test_reads_work_without_a_startup_event(self):
        response = await self.client.get(f"{BASE}/devices", headers=await self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_writes_work_without_a_startup_event(self):
        response = await self.client.post(
            f"{BASE}/ingest/location", json=FIX, headers={"X-API-Key": API_KEY}
        )
        self.assertEqual(response.status_code, 202)

    async def test_health_reports_the_backend_without_a_startup_event(self):
        response = await self.client.get(f"{BASE}/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["database"], "ok")

    async def test_the_repository_is_built_once_and_reused(self):
        await self.client.post(
            f"{BASE}/ingest/location", json=FIX, headers={"X-API-Key": API_KEY}
        )
        # A second request must see the first request's data, i.e. the same
        # repository instance -- not a fresh one built per request.
        response = await self.client.get(f"{BASE}/devices/dev1/latest", headers=await self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()["latitude"], 12.97)

    async def test_concurrent_cold_start_requests_share_one_repository(self):
        import asyncio

        await asyncio.gather(*(self.client.get(f"{BASE}/devices") for _ in range(10)))
        app = self.client.app
        self.assertIsNotNone(app.state.repository)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestUnreachableStorage(unittest.IsolatedAsyncioTestCase):
    async def test_a_bad_dsn_answers_503_rather_than_crashing(self):
        app = create_app(
            ApiConfig(backend="postgres", pg_dsn="postgresql://nobody@127.0.0.1:1/none")
        )
        client = AsgiClient(app)
        response = await client.get(f"{BASE}/devices")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Storage unavailable")


if __name__ == "__main__":
    unittest.main()
