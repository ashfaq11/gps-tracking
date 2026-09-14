import unittest
from datetime import datetime, timedelta, timezone

try:
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

API_KEY = "test-secret"
BASE = "/api/v1"

FIX = {
    "device_id": "868120303372449",
    "latitude": 12.971598,
    "longitude": 77.594566,
    "speed_kmh": 42,
    "course_deg": 15,
    "gps_fixed": True,
    "satellites": 9,
}


ADMIN = {"username": "admin", "password": "adminpassword"}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    def make_app(self, **overrides):
        config = ApiConfig(
            backend="memory",
            ingest_api_key=API_KEY,
            bootstrap_admin_username=ADMIN["username"],
            bootstrap_admin_password=ADMIN["password"],
            **overrides,
        )
        return create_app(config)

    async def asyncSetUp(self):
        self.client = AsgiClient(self.make_app())
        await self.client.__aenter__()
        self.token = await self.login(self.client)

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    @staticmethod
    async def login(client) -> str:
        response = await client.post(f"{BASE}/auth/login", json=ADMIN)
        return response.json()["token"]

    def auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    async def ingest(self, payload=None, key=API_KEY):
        headers = {"X-API-Key": key} if key is not None else None
        return await self.client.post(
            f"{BASE}/ingest/location", json=payload or FIX, headers=headers
        )


class TestHealth(ApiTestCase):
    async def test_health_reports_backend_and_database(self):
        response = await self.client.get(f"{BASE}/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "backend": "memory", "database": "ok"})


class TestIngestAuth(ApiTestCase):
    async def test_accepts_a_fix_with_a_valid_key(self):
        response = await self.ingest()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["device_id"], FIX["device_id"])

    async def test_rejects_a_missing_key(self):
        self.assertEqual((await self.ingest(key=None)).status_code, 401)

    async def test_rejects_a_wrong_key(self):
        self.assertEqual((await self.ingest(key="nope")).status_code, 401)

    async def test_ingest_is_disabled_when_no_key_is_configured(self):
        # An unauthenticated ingest path would let anyone forge positions.
        client = AsgiClient(create_app(ApiConfig(backend="memory", ingest_api_key=None)))
        async with client:
            response = await client.post(f"{BASE}/ingest/location", json=FIX)
        self.assertEqual(response.status_code, 503)


class TestIngestValidation(ApiTestCase):
    async def test_rejects_an_out_of_range_latitude(self):
        response = await self.ingest({**FIX, "latitude": 91.0})
        self.assertEqual(response.status_code, 422)

    async def test_rejects_an_out_of_range_longitude(self):
        response = await self.ingest({**FIX, "longitude": -181.0})
        self.assertEqual(response.status_code, 422)

    async def test_rejects_an_empty_device_id(self):
        response = await self.ingest({**FIX, "device_id": ""})
        self.assertEqual(response.status_code, 422)

    async def test_rejects_a_missing_required_field(self):
        response = await self.ingest({"device_id": "x", "latitude": 1.0})
        self.assertEqual(response.status_code, 422)

    async def test_optional_fields_get_defaults(self):
        await self.ingest({"device_id": "d9", "latitude": 1.0, "longitude": 2.0})
        response = await self.client.get(f"{BASE}/devices/d9/latest", headers=self.auth())
        body = response.json()
        self.assertEqual(body["speed_kmh"], 0)
        self.assertTrue(body["gps_fixed"])


class TestReadEndpoints(ApiTestCase):
    async def test_latest_returns_the_most_recent_fix(self):
        await self.ingest()
        await self.ingest({**FIX, "latitude": 13.5})
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/latest", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()["latitude"], 13.5)

    async def test_latest_is_404_for_an_unknown_device(self):
        response = await self.client.get(f"{BASE}/devices/nosuchdevice/latest", headers=self.auth())
        self.assertEqual(response.status_code, 404)

    async def test_history_is_newest_first(self):
        for lat in (1.0, 2.0, 3.0):
            await self.ingest({**FIX, "latitude": lat})
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations", headers=self.auth()
        )
        latitudes = [row["latitude"] for row in response.json()]
        self.assertEqual(latitudes, [3.0, 2.0, 1.0])

    async def test_history_respects_the_limit(self):
        for lat in (1.0, 2.0, 3.0):
            await self.ingest({**FIX, "latitude": lat})
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations?limit=2", headers=self.auth()
        )
        self.assertEqual(len(response.json()), 2)

    async def test_history_limit_is_clamped_to_the_configured_maximum(self):
        client = AsgiClient(
            create_app(
                ApiConfig(
                    backend="memory",
                    ingest_api_key=API_KEY,
                    max_page_size=1,
                    bootstrap_admin_username=ADMIN["username"],
                    bootstrap_admin_password=ADMIN["password"],
                )
            )
        )
        async with client:
            token = await self.login(client)
            for lat in (1.0, 2.0):
                await client.post(
                    f"{BASE}/ingest/location",
                    json={**FIX, "latitude": lat},
                    headers={"X-API-Key": API_KEY},
                )
            response = await client.get(
                f"{BASE}/devices/{FIX['device_id']}/locations?limit=10000",
                headers=self.auth(token),
            )
        self.assertEqual(len(response.json()), 1)

    async def test_history_rejects_a_non_positive_limit(self):
        response = await self.client.get(f"{BASE}/devices/d/locations?limit=0", headers=self.auth())
        self.assertEqual(response.status_code, 422)

    async def test_history_is_empty_for_an_unknown_device(self):
        response = await self.client.get(
            f"{BASE}/devices/nosuchdevice/locations", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def _three_fixes(self) -> list[dict]:
        """Ingest three fixes and return them newest-first, with timestamps."""
        for lat in (1.0, 2.0, 3.0):
            await self.ingest({**FIX, "latitude": lat})
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations", headers=self.auth()
        )
        return response.json()

    async def test_history_since_excludes_earlier_fixes(self):
        rows = await self._three_fixes()
        middle = rows[1]["received_at"]
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations?since={middle}", headers=self.auth()
        )
        # `since` is inclusive, so the middle fix itself is kept.
        self.assertEqual([r["latitude"] for r in response.json()], [3.0, 2.0])

    async def test_history_until_excludes_later_fixes(self):
        rows = await self._three_fixes()
        middle = rows[1]["received_at"]
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations?until={middle}", headers=self.auth()
        )
        self.assertEqual([r["latitude"] for r in response.json()], [2.0, 1.0])

    async def test_history_between_since_and_until_is_a_closed_range(self):
        rows = await self._three_fixes()
        middle = rows[1]["received_at"]
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations?since={middle}&until={middle}",
            headers=self.auth(),
        )
        # Both bounds inclusive, so an equal pair returns exactly that fix.
        self.assertEqual([r["latitude"] for r in response.json()], [2.0])

    async def test_history_rejects_an_inverted_range(self):
        # An empty list would read as "the vehicle reported nothing", which is
        # a very different answer from "your dates are the wrong way round".
        response = await self.client.get(
            f"{BASE}/devices/d/locations"
            "?since=2026-09-02T00:00:00Z&until=2026-09-01T00:00:00Z",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 422)

    async def test_history_rejects_a_malformed_until(self):
        response = await self.client.get(
            f"{BASE}/devices/d/locations?until=not-a-date", headers=self.auth()
        )
        self.assertEqual(response.status_code, 422)

    async def test_device_list_summarises_each_device(self):
        await self.ingest()
        await self.ingest()
        await self.ingest({**FIX, "device_id": "other"})
        response = await self.client.get(f"{BASE}/devices", headers=self.auth())
        summary = {row["device_id"]: row["fix_count"] for row in response.json()}
        self.assertEqual(summary, {FIX["device_id"]: 2, "other": 1})

    async def test_device_list_is_empty_before_any_ingest(self):
        response = await self.client.get(f"{BASE}/devices", headers=self.auth())
        self.assertEqual(response.json(), [])


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class TestDeviceSubscriptions(ApiTestCase):
    async def test_onboarding_with_no_end_date_defaults_to_one_year(self):
        response = await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription", json={}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        end_date = datetime.fromisoformat(
            response.json()["subscription_end_date"].replace("Z", "+00:00")
        )
        expected = datetime.now(timezone.utc) + timedelta(days=365)
        self.assertLess(abs((end_date - expected).total_seconds()), 5)

    async def test_onboarding_with_no_request_body_also_defaults_to_one_year(self):
        response = await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["subscription_end_date"])

    async def test_a_device_with_no_subscription_is_unmetered(self):
        await self.ingest()
        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/subscription", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["subscription_end_date"])
        self.assertEqual(response.json()["subscription_status"], "active")

    async def test_a_non_admin_cannot_set_a_subscription(self):
        await self.client.post(
            f"{BASE}/users",
            json={"username": "dispatcher", "password": "dispatch123", "role": "user"},
            headers=self.auth(),
        )
        token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
            )
        ).json()["token"]

        future = _iso(datetime.now(timezone.utc) + timedelta(days=30))
        response = await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": future},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 403)

    async def test_an_expired_device_disappears_from_latest_and_history_but_not_the_list(self):
        await self.ingest()
        past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": past},
            headers=self.auth(),
        )

        latest = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/latest", headers=self.auth()
        )
        self.assertEqual(latest.status_code, 404)

        history = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations", headers=self.auth()
        )
        self.assertEqual(history.json(), [])

        # Still discoverable, and visibly expired, so an admin can renew it.
        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertEqual(row["subscription_status"], "expired")
        self.assertEqual(row["fix_count"], 1)

    async def test_renewing_restores_access(self):
        await self.ingest()
        past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        future = _iso(datetime.now(timezone.utc) + timedelta(days=30))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": past},
            headers=self.auth(),
        )
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": future},
            headers=self.auth(),
        )

        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/latest", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)

    async def test_renewals_are_recorded_in_history_newest_first(self):
        first = _iso(datetime.now(timezone.utc) + timedelta(days=30))
        second = _iso(datetime.now(timezone.utc) + timedelta(days=60))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": first},
            headers=self.auth(),
        )
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": second},
            headers=self.auth(),
        )

        response = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/subscription-history", headers=self.auth()
        )
        entries = response.json()
        self.assertEqual(len(entries), 2)
        self.assertIsNone(entries[1]["previous_end_date"])  # first change, from unmetered
        self.assertEqual(entries[0]["previous_end_date"], entries[1]["new_end_date"])
        # changed_by_username needs a join to the users table; the in-memory
        # backend has no reference to it (see repository.py), so only the id
        # round-trips here -- Postgres resolves the username too.
        admin_id = (await self.client.get(f"{BASE}/auth/me", headers=self.auth())).json()["id"]
        self.assertEqual(entries[0]["changed_by"], admin_id)

    async def test_clearing_lifts_metering_and_restores_access(self):
        await self.ingest()
        past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": past},
            headers=self.auth(),
        )

        response = await self.client.delete(
            f"{BASE}/devices/{FIX['device_id']}/subscription", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subscription_status"], "active")

        latest = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/latest", headers=self.auth()
        )
        self.assertEqual(latest.status_code, 200)

    async def test_clearing_an_unmetered_device_is_a_404(self):
        response = await self.client.delete(
            f"{BASE}/devices/{FIX['device_id']}/subscription", headers=self.auth()
        )
        self.assertEqual(response.status_code, 404)

    async def test_expired_devices_are_excluded_from_stats(self):
        await self.ingest()
        past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": past},
            headers=self.auth(),
        )

        response = await self.client.get(f"{BASE}/stats/summary", headers=self.auth())
        self.assertEqual(response.json()["devices_total"], 0)
        self.assertEqual(response.json()["fixes_total"], 0)

    async def test_installed_at_and_sim_expiry_date_round_trip(self):
        installed = _iso(datetime.now(timezone.utc) - timedelta(days=200))
        sim_expiry = _iso(datetime.now(timezone.utc) + timedelta(days=100))
        response = await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"installed_at": installed, "sim_expiry_date": sim_expiry},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["installed_at"], installed)
        self.assertEqual(body["sim_expiry_date"], sim_expiry)
        # subscription_end_date still gets its onboarding default even though
        # this call was only about the other two dates.
        self.assertIsNotNone(body["subscription_end_date"])

    async def test_installed_at_persists_when_only_renewing_the_subscription(self):
        installed = _iso(datetime.now(timezone.utc) - timedelta(days=200))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"installed_at": installed},
            headers=self.auth(),
        )

        future = _iso(datetime.now(timezone.utc) + timedelta(days=30))
        response = await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"subscription_end_date": future},
            headers=self.auth(),
        )
        # installed_at was omitted this time, so it must not be wiped.
        self.assertEqual(response.json()["installed_at"], installed)
        self.assertEqual(response.json()["subscription_end_date"], future)

    async def test_clearing_the_subscription_keeps_installed_at_and_sim_expiry(self):
        installed = _iso(datetime.now(timezone.utc) - timedelta(days=200))
        sim_expiry = _iso(datetime.now(timezone.utc) + timedelta(days=100))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"installed_at": installed, "sim_expiry_date": sim_expiry},
            headers=self.auth(),
        )

        response = await self.client.delete(
            f"{BASE}/devices/{FIX['device_id']}/subscription", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["subscription_end_date"])
        self.assertEqual(response.json()["installed_at"], installed)
        self.assertEqual(response.json()["sim_expiry_date"], sim_expiry)

    async def test_device_list_includes_installed_at_and_sim_expiry(self):
        await self.ingest()
        installed = _iso(datetime.now(timezone.utc) - timedelta(days=200))
        await self.client.put(
            f"{BASE}/devices/{FIX['device_id']}/subscription",
            json={"installed_at": installed},
            headers=self.auth(),
        )

        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertEqual(row["installed_at"], installed)
        self.assertIsNone(row["sim_expiry_date"])


class TestDeviceProfile(ApiTestCase):
    async def test_unconfigured_device_defaults_to_a_car(self):
        await self.ingest()
        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertEqual(row["icon"], "car")
        self.assertIsNone(row["name"])

    async def test_setting_name_and_icon_reflects_in_the_device_list(self):
        await self.ingest()
        response = await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}",
            json={"name": "Delivery Van 3", "icon": "van"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "device_id": FIX["device_id"],
            "name": "Delivery Van 3",
            "icon": "van",
        })

        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertEqual(row["name"], "Delivery Van 3")
        self.assertEqual(row["icon"], "van")

    async def test_works_before_the_device_has_ever_reported(self):
        # No ingest -- same "device_id need not exist yet" reasoning as the
        # subscription endpoints.
        response = await self.client.patch(
            f"{BASE}/devices/never-seen-yet", json={"icon": "truck"}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["icon"], "truck")

    async def test_omitting_a_field_leaves_it_unchanged(self):
        await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}",
            json={"name": "Van 3", "icon": "van"},
            headers=self.auth(),
        )
        response = await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}", json={"icon": "truck"}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Van 3")
        self.assertEqual(response.json()["icon"], "truck")

    async def test_an_empty_name_clears_it(self):
        await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}", json={"name": "Van 3"}, headers=self.auth()
        )
        response = await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}", json={"name": ""}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["name"])

    async def test_a_non_admin_may_rename_a_device_assigned_to_them(self):
        await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": [FIX["device_id"]],
            },
            headers=self.auth(),
        )
        token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
            )
        ).json()["token"]

        response = await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}",
            json={"icon": "bike"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["icon"], "bike")

    async def test_a_scoped_account_cannot_touch_an_unassigned_device(self):
        await self.client.post(
            f"{BASE}/users",
            json={"username": "dispatcher", "password": "dispatch123", "role": "user", "devices": []},
            headers=self.auth(),
        )
        token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
            )
        ).json()["token"]

        response = await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}",
            json={"icon": "bike"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 404)

    async def test_an_invalid_icon_is_rejected(self):
        response = await self.client.patch(
            f"{BASE}/devices/{FIX['device_id']}",
            json={"icon": "spaceship"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 422)


class TestHaltedSince(ApiTestCase):
    """GET /devices exposes halted_since -- when a device was last moving."""

    async def test_a_device_that_has_never_moved_is_halted_since_its_first_fix(self):
        await self.ingest({**FIX, "speed_kmh": 0})
        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertIsNotNone(row["halted_since"])

    async def test_halted_since_is_the_last_moving_fix_not_the_current_one(self):
        await self.ingest({**FIX, "speed_kmh": 40})
        moving = await self.client.get(
            f"{BASE}/devices/{FIX['device_id']}/locations", headers=self.auth()
        )
        moving_at = moving.json()[0]["received_at"]

        await self.ingest({**FIX, "speed_kmh": 0})
        await self.ingest({**FIX, "speed_kmh": 0})  # a second halted fix changes nothing

        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        # Not the newest fix's own time (that would read as "just stopped"
        # forever) -- the moving one three fixes back.
        self.assertEqual(row["halted_since"], moving_at)
        self.assertNotEqual(row["halted_since"], row["last_seen"])

    async def test_halted_since_is_null_for_no_history(self):
        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        self.assertEqual(listing.json(), [])


class TestDeviceOwnership(ApiTestCase):
    """GET /devices exposes owner_username -- who has claimed each device."""

    async def test_an_unclaimed_device_has_no_owner(self):
        await self.ingest()
        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertIsNone(row["owner_username"])

    async def test_a_claimed_device_shows_its_owner(self):
        await self.ingest()
        await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "priya",
                "password": "a-good-passphrase",
                "device_id": FIX["device_id"],
            },
        )
        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        row = next(r for r in listing.json() if r["device_id"] == FIX["device_id"])
        self.assertEqual(row["owner_username"], "priya")

    async def test_a_scoped_account_sees_the_owner_of_a_shared_device(self):
        await self.ingest()
        await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "priya",
                "password": "a-good-passphrase",
                "device_id": FIX["device_id"],
            },
        )
        # Admin shares visibility with a second account without taking
        # ownership from priya.
        await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": [FIX["device_id"]],
            },
            headers=self.auth(),
        )
        token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
            )
        ).json()["token"]

        listing = await self.client.get(
            f"{BASE}/devices", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(listing.json()[0]["owner_username"], "priya")


class TestAdminOnboarding(ApiTestCase):
    """Admin assigning a device via POST/PATCH /users also claims it --
    the admin equivalent of self-service signup's claim step."""

    async def test_creating_a_user_with_a_device_claims_it(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        response = await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": ["dev-a"],
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201)

        # Claimed, not just visible: self-service signup for the same
        # device now conflicts rather than succeeding.
        signup = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "stranger", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        self.assertEqual(signup.status_code, 409)

    async def test_can_onboard_a_device_that_has_never_reported(self):
        response = await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": ["never-seen-yet"],
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201)

        token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
            )
        ).json()["token"]
        listing = await self.client.get(f"{BASE}/devices", headers={"Authorization": f"Bearer {token}"})
        # Listed even though it has never reported: last_seen/fix_count say
        # plainly that it hasn't, but the claim is real, proven below by
        # pre-empting a stranger's signup for the same id.
        self.assertEqual(
            listing.json(),
            [
                {
                    "device_id": "never-seen-yet",
                    "last_seen": None,
                    "fix_count": 0,
                    "subscription_end_date": None,
                    "subscription_status": "active",
                    "installed_at": None,
                    "sim_expiry_date": None,
                    "name": None,
                    "icon": "car",
                    "halted_since": None,
                    "owner_username": "dispatcher",
                }
            ],
        )

        await self.ingest({**FIX, "device_id": "never-seen-yet"}, key=API_KEY)
        signup = await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "stranger",
                "password": "a-good-passphrase",
                "device_id": "never-seen-yet",
            },
        )
        self.assertEqual(signup.status_code, 409)

    async def test_assigning_an_already_owned_device_does_not_steal_it(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        owner_signup = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "owner", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        owner_token = owner_signup.json()["token"]

        # Admin grants a second account visibility into the same device --
        # sharing it, e.g. a dispatcher -- without taking it from its owner.
        response = await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": ["dev-a"],
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201)

        # The owner still sees it too -- visibility was shared, not moved.
        owner_devices = await self.client.get(
            f"{BASE}/devices", headers={"Authorization": f"Bearer {owner_token}"}
        )
        self.assertEqual([d["device_id"] for d in owner_devices.json()], ["dev-a"])

    async def test_a_named_but_unclaimed_device_shows_up_for_an_admin(self):
        # Naming a device (the "just register it" path with no owner picked)
        # never touches device_claims, so this device is neither reported
        # nor claimed -- it must still be findable, or an admin who just
        # added it has no way to see it worked.
        response = await self.client.patch(
            f"{BASE}/devices/pre-provisioned-1", json={"name": "Truck 12"}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)

        listing = await self.client.get(f"{BASE}/devices", headers=self.auth())
        rows = {d["device_id"]: d for d in listing.json()}
        self.assertIn("pre-provisioned-1", rows)
        row = rows["pre-provisioned-1"]
        self.assertEqual(row["name"], "Truck 12")
        self.assertIsNone(row["last_seen"])
        self.assertEqual(row["fix_count"], 0)
        self.assertIsNone(row["owner_username"])

        # And it stays completely invisible to an unrelated account -- an
        # admin's inventory is not everyone's business.
        await self.client.post(
            f"{BASE}/users",
            json={"username": "bystander", "password": "bystander123", "role": "user"},
            headers=self.auth(),
        )
        bystander_token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "bystander", "password": "bystander123"}
            )
        ).json()["token"]
        bystander_listing = await self.client.get(
            f"{BASE}/devices", headers={"Authorization": f"Bearer {bystander_token}"}
        )
        self.assertEqual(bystander_listing.json(), [])

    async def test_a_claimed_but_unreported_device_shows_up_for_its_owner(self):
        # The same gap for a device that has an owner but, unlike the tests
        # above, was never ingested at all -- the owner's own account must
        # see it too, not just an admin looking at the whole fleet.
        response = await self.client.post(
            f"{BASE}/users",
            json={
                "username": "earlybird",
                "password": "earlybird123",
                "role": "user",
                "devices": ["shipping-soon"],
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201)

        token = (
            await self.client.post(
                f"{BASE}/auth/login", json={"username": "earlybird", "password": "earlybird123"}
            )
        ).json()["token"]
        listing = await self.client.get(f"{BASE}/devices", headers={"Authorization": f"Bearer {token}"})
        rows = {d["device_id"]: d for d in listing.json()}
        self.assertIn("shipping-soon", rows)
        self.assertEqual(rows["shipping-soon"]["owner_username"], "earlybird")
        self.assertIsNone(rows["shipping-soon"]["last_seen"])


class TestSignupAndClaim(ApiTestCase):
    async def test_signup_creates_an_account_and_claims_a_reporting_device(self):
        await self.ingest()  # FIX['device_id'] has now reported

        response = await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "priya",
                "password": "a-good-passphrase",
                "email": "priya@example.com",
                "mobile": "+919876543210",
                "device_id": FIX["device_id"],
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["user"]["username"], "priya")
        self.assertEqual(body["user"]["role"], "user")
        self.assertEqual(body["user"]["email"], "priya@example.com")
        self.assertIn("token", body)

        # Signed in as the new account, only its own device is visible.
        listing = await self.client.get(
            f"{BASE}/devices", headers={"Authorization": f"Bearer {body['token']}"}
        )
        self.assertEqual([d["device_id"] for d in listing.json()], [FIX["device_id"]])

    async def test_signup_rejects_a_device_that_has_never_reported(self):
        response = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "priya", "password": "a-good-passphrase", "device_id": "ghost-imei"},
        )
        self.assertEqual(response.status_code, 404)

    async def test_signup_rejects_a_device_already_claimed(self):
        await self.ingest()
        await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "priya",
                "password": "a-good-passphrase",
                "device_id": FIX["device_id"],
            },
        )
        response = await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "rahul",
                "password": "a-good-passphrase",
                "device_id": FIX["device_id"],
            },
        )
        self.assertEqual(response.status_code, 409)
        # And the account that lost the race was still not created.
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "rahul", "password": "a-good-passphrase"}
        )
        self.assertEqual(login.status_code, 401)

    async def test_signup_rejects_a_duplicate_username_or_email(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        await self.ingest({**FIX, "device_id": "dev-b"}, key=API_KEY)
        await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "priya",
                "password": "a-good-passphrase",
                "email": "priya@example.com",
                "device_id": "dev-a",
            },
        )

        same_username = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "priya", "password": "another-passphrase", "device_id": "dev-b"},
        )
        self.assertEqual(same_username.status_code, 409)

        same_email = await self.client.post(
            f"{BASE}/auth/signup",
            json={
                "username": "someoneelse",
                "password": "another-passphrase",
                "email": "priya@example.com",
                "device_id": "dev-b",
            },
        )
        self.assertEqual(same_email.status_code, 409)

    async def test_a_signed_in_account_can_claim_a_second_device(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        await self.ingest({**FIX, "device_id": "dev-b"}, key=API_KEY)
        signup = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "priya", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        token = signup.json()["token"]

        response = await self.client.post(
            f"{BASE}/devices/claim",
            json={"device_id": "dev-b"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 201)

        listing = await self.client.get(
            f"{BASE}/devices", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(
            sorted(d["device_id"] for d in listing.json()), ["dev-a", "dev-b"]
        )

    async def test_claiming_an_owned_device_conflicts(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        await self.ingest({**FIX, "device_id": "dev-b"}, key=API_KEY)
        await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "priya", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        second = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "rahul", "password": "a-good-passphrase", "device_id": "dev-b"},
        )
        token = second.json()["token"]  # rahul owns dev-b

        response = await self.client.post(
            f"{BASE}/devices/claim",
            json={"device_id": "dev-a"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 409)  # dev-a already belongs to priya

    async def test_admin_can_release_a_device_for_a_new_owner(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        signup = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "priya", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        priya_token = signup.json()["token"]

        # Not yet released: a second signup for the same device still conflicts.
        blocked = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "rahul", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        self.assertEqual(blocked.status_code, 409)

        released = await self.client.delete(
            f"{BASE}/devices/dev-a/claim", headers=self.auth()
        )
        self.assertEqual(released.status_code, 204)

        # Now a new owner can claim it.
        resold = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "rahul", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        self.assertEqual(resold.status_code, 201)

        # And priya no longer sees it.
        priya_devices = await self.client.get(
            f"{BASE}/devices", headers={"Authorization": f"Bearer {priya_token}"}
        )
        self.assertEqual(priya_devices.json(), [])

    async def test_releasing_an_unclaimed_device_is_404(self):
        response = await self.client.delete(f"{BASE}/devices/never-claimed/claim", headers=self.auth())
        self.assertEqual(response.status_code, 404)

    async def test_a_non_admin_cannot_release_a_device(self):
        await self.ingest({**FIX, "device_id": "dev-a"}, key=API_KEY)
        signup = await self.client.post(
            f"{BASE}/auth/signup",
            json={"username": "priya", "password": "a-good-passphrase", "device_id": "dev-a"},
        )
        token = signup.json()["token"]

        response = await self.client.delete(
            f"{BASE}/devices/dev-a/claim", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 403)


class TestOpenApi(ApiTestCase):
    async def test_openapi_schema_is_served(self):
        response = await self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"{BASE}/ingest/location", response.json()["paths"])


if __name__ == "__main__":
    unittest.main()
