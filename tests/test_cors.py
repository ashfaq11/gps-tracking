"""
CORS for the native apps: the Android WebView calls the API from
http://localhost and the iOS one from capacitor://localhost, both
cross-origin, so every method the dashboard uses must pass the preflight.
"""

import unittest

try:
    from api.config import ApiConfig
    from api.main import cors_allow_origins, create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

BASE = "/api/v1"


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestCors(unittest.IsolatedAsyncioTestCase):
    async def preflight(self, origin, method, configured=("http://localhost",)):
        app = create_app(ApiConfig(backend="memory", cors_origins=list(configured)))
        async with AsgiClient(app) as client:
            return await client.request(
                "OPTIONS",
                f"{BASE}/trackers/settings",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "authorization",
                },
            )

    async def test_the_ios_app_is_allowed_alongside_the_configured_origins(self):
        response = await self.preflight("capacitor://localhost", "GET")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"), "capacitor://localhost"
        )

    async def test_put_passes_the_preflight(self):
        # Tracker settings, engine cut-off and subscriptions are PUTs.
        response = await self.preflight("http://localhost", "PUT")
        self.assertEqual(response.status_code, 200)
        self.assertIn("PUT", response.headers.get("access-control-allow-methods", ""))

    async def test_an_unlisted_website_is_still_refused(self):
        response = await self.preflight("https://evil.example", "GET")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_the_app_origin_is_added_once(self):
        self.assertEqual(
            cors_allow_origins(["capacitor://localhost", "http://localhost"]),
            ["capacitor://localhost", "http://localhost"],
        )
        self.assertEqual(
            cors_allow_origins(["http://localhost"]), ["http://localhost", "capacitor://localhost"]
        )


if __name__ == "__main__":
    unittest.main()
