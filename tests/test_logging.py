import logging
import unittest

try:
    from fastapi import HTTPException

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
class LoggingTestCase(unittest.IsolatedAsyncioTestCase):
    def build(self, **overrides):
        return create_app(
            ApiConfig(
                backend="memory",
                ingest_api_key=API_KEY,
                bootstrap_admin_username=ADMIN["username"],
                bootstrap_admin_password=ADMIN["password"],
                **overrides,
            )
        )

    async def asyncSetUp(self):
        self.client = AsgiClient(self.build())
        await self.client.__aenter__()
        login = await self.client.post(f"{BASE}/auth/login", json=ADMIN)
        self.token = login.json()["token"]

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}


class TestRequestId(LoggingTestCase):
    async def test_every_response_carries_a_request_id_header(self):
        response = await self.client.get(f"{BASE}/devices")
        self.assertIn("x-request-id", response.headers)
        self.assertEqual(len(response.headers["x-request-id"]), 12)

    async def test_request_ids_are_unique_per_request(self):
        first = await self.client.get(f"{BASE}/devices")
        second = await self.client.get(f"{BASE}/devices")
        self.assertNotEqual(first.headers["x-request-id"], second.headers["x-request-id"])

    async def test_the_logged_id_matches_the_header(self):
        with self.assertLogs("api.access", level="INFO") as captured:
            response = await self.client.get(f"{BASE}/devices")
        self.assertIn(response.headers["x-request-id"], captured.records[0].request_id)


class TestAccessLog(LoggingTestCase):
    async def test_logs_method_path_status_and_duration(self):
        with self.assertLogs("api.access", level="INFO") as captured:
            await self.client.get(f"{BASE}/devices", headers=self.auth())
        line = captured.output[0]
        self.assertIn("GET /api/v1/devices", line)
        self.assertIn("-> 200", line)
        self.assertIn("ms", line)

    async def test_includes_the_query_string(self):
        with self.assertLogs("api.access", level="INFO") as captured:
            await self.client.get(f"{BASE}/devices/dev1/locations?limit=5")
        self.assertIn("?limit=5", captured.output[0])

    async def test_health_checks_are_logged_at_debug_not_info(self):
        # Load balancers poll this constantly; at INFO it drowns real traffic.
        with self.assertLogs("api.access", level="DEBUG") as captured:
            await self.client.get(f"{BASE}/health")
        self.assertEqual(captured.records[0].levelno, logging.DEBUG)

    async def test_client_errors_are_logged_as_warnings(self):
        with self.assertLogs("api.access", level="INFO") as captured:
            await self.client.get(f"{BASE}/devices/nope/latest")
        self.assertEqual(captured.records[0].levelno, logging.WARNING)

    async def test_slow_requests_are_logged_as_warnings(self):
        client = AsgiClient(self.build(slow_request_ms=0.0))
        async with client:
            with self.assertLogs("api.access", level="INFO") as captured:
                await client.get(f"{BASE}/devices")
        self.assertEqual(captured.records[0].levelno, logging.WARNING)


class TestErrorLogging(LoggingTestCase):
    async def test_401_is_logged_with_its_reason(self):
        with self.assertLogs("api.error", level="WARNING") as captured:
            response = await self.client.post(f"{BASE}/ingest/location", json=FIX)
        self.assertEqual(response.status_code, 401)
        self.assertIn("X-API-Key", captured.output[0])

    async def test_404_is_logged_with_its_detail(self):
        with self.assertLogs("api.error", level="WARNING") as captured:
            await self.client.get(f"{BASE}/devices/nope/latest", headers=self.auth())
        self.assertIn("No fixes for device nope", captured.output[0])

    async def test_422_logs_which_fields_failed(self):
        with self.assertLogs("api.error", level="WARNING") as captured:
            await self.client.post(
                f"{BASE}/ingest/location",
                json={**FIX, "latitude": 91},
                headers={"X-API-Key": API_KEY},
            )
        self.assertIn("body.latitude", captured.output[0])

    async def test_error_responses_carry_the_request_id(self):
        response = await self.client.get(f"{BASE}/devices/nope/latest")
        body = response.json()
        self.assertEqual(body["request_id"], response.headers["x-request-id"])

    async def test_422_body_names_the_failing_fields(self):
        response = await self.client.post(
            f"{BASE}/ingest/location",
            json={**FIX, "latitude": 91},
            headers={"X-API-Key": API_KEY},
        )
        fields = [problem["field"] for problem in response.json()["detail"]]
        self.assertIn("body.latitude", fields)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestUnhandledException(unittest.IsolatedAsyncioTestCase):
    async def test_a_crashing_endpoint_is_logged_with_a_traceback(self):
        app = create_app(ApiConfig(backend="memory", ingest_api_key=API_KEY))

        @app.get("/boom")
        async def boom():
            raise RuntimeError("database went away")

        client = AsgiClient(app)
        async with client:
            with self.assertLogs("api.error", level="ERROR") as captured:
                response = await client.get("/boom")
        record = captured.records[0]
        self.assertIn("500 unhandled RuntimeError", record.getMessage())
        self.assertIsNotNone(record.exc_info, "traceback must be logged")
        self.assertEqual(response.status_code, 500)

    async def test_a_500_does_not_leak_internal_detail(self):
        app = create_app(ApiConfig(backend="memory", ingest_api_key=API_KEY))

        @app.get("/boom")
        async def boom():
            raise RuntimeError("connection string user:password@host")

        client = AsgiClient(app)
        async with client:
            with self.assertLogs("api.error", level="ERROR"):
                response = await client.get("/boom")
        self.assertEqual(response.json()["detail"], "Internal server error")
        self.assertNotIn("password", response.body.decode())

    async def test_a_500_still_carries_a_usable_request_id(self):
        # The contextvar is already reset when the 500 handler runs, so this
        # guards the scope-based fallback.
        app = create_app(ApiConfig(backend="memory", ingest_api_key=API_KEY))

        @app.get("/boom")
        async def boom():
            raise RuntimeError("kaboom")

        client = AsgiClient(app)
        async with client:
            with self.assertLogs("api.error", level="ERROR") as captured:
                response = await client.get("/boom")
        request_id = response.json()["request_id"]
        self.assertNotEqual(request_id, "-")
        self.assertEqual(response.headers["x-request-id"], request_id)
        self.assertEqual(captured.records[0].request_id, request_id)

    async def test_an_http_exception_detail_is_not_swallowed(self):
        app = create_app(ApiConfig(backend="memory", ingest_api_key=API_KEY))

        @app.get("/teapot")
        async def teapot():
            raise HTTPException(status_code=418, detail="I am a teapot")

        client = AsgiClient(app)
        async with client:
            with self.assertLogs("api.error", level="WARNING"):
                response = await client.get("/teapot")
        self.assertEqual(response.status_code, 418)
        self.assertEqual(response.json()["detail"], "I am a teapot")


if __name__ == "__main__":
    unittest.main()
