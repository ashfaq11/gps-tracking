"""Web Push subscriptions: the vapid-public-key, subscribe and unsubscribe endpoints."""

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
VAPID_PUBLIC = "BK55f33iTFofr0vAv_9l0utN_rbLgnn7MdlQpvFwBsN6Vo6-OJ1ZQVX0B8jr3dR6mTtv_jRMxUV7rSqwt4ev87M"
VAPID_PRIVATE = "BBJEzn-3ioQOeTiAkXlIn2Y45Nt28A4qa27E7I-ejS0"

SUBSCRIPTION = {
    "endpoint": "https://push.example.com/send/abc123",
    "keys": {"p256dh": "a-fake-p256dh-key", "auth": "a-fake-auth-secret"},
}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class PushTestCase(unittest.IsolatedAsyncioTestCase):
    def make_app(self, *, configured: bool = True, **overrides):
        config = ApiConfig(
            backend="memory",
            bootstrap_admin_username=ADMIN["username"],
            bootstrap_admin_password=ADMIN["password"],
            vapid_public_key=VAPID_PUBLIC if configured else None,
            vapid_private_key=VAPID_PRIVATE if configured else None,
            **overrides,
        )
        return create_app(config)

    async def asyncSetUp(self):
        self.client = AsgiClient(self.make_app())
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def admin_token(self) -> str:
        response = await self.client.post(
            f"{BASE}/auth/login", json={"username": ADMIN["username"], "password": ADMIN["password"]}
        )
        return response.json()["token"]

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}


class TestVapidPublicKey(PushTestCase):
    async def test_returns_the_configured_key_with_no_sign_in_required(self):
        response = await self.client.get(f"{BASE}/push/vapid-public-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["public_key"], VAPID_PUBLIC)

    async def test_503_when_push_is_not_configured(self):
        client = AsgiClient(self.make_app(configured=False))
        async with client:
            response = await client.get(f"{BASE}/push/vapid-public-key")
        self.assertEqual(response.status_code, 503)


class TestSubscribe(PushTestCase):
    async def test_requires_sign_in(self):
        response = await self.client.post(f"{BASE}/push/subscribe", json=SUBSCRIPTION)
        self.assertEqual(response.status_code, 401)

    async def test_accepts_a_subscription_from_a_signed_in_account(self):
        token = await self.admin_token()
        response = await self.client.post(
            f"{BASE}/push/subscribe", json=SUBSCRIPTION, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 204)

    async def test_503_when_push_is_not_configured(self):
        client = AsgiClient(self.make_app(configured=False))
        async with client:
            login = await client.post(f"{BASE}/auth/login", json=ADMIN | {"password": ADMIN["password"]})
            token = login.json()["token"]
            response = await client.post(
                f"{BASE}/push/subscribe", json=SUBSCRIPTION, headers=self.auth(token)
            )
        self.assertEqual(response.status_code, 503)

    async def test_an_admin_subscription_is_reachable_by_device_id(self):
        # An admin sees every device, so their subscription must come back
        # for any device_id at all -- there is nothing to assign.
        token = await self.admin_token()
        await self.client.post(f"{BASE}/push/subscribe", json=SUBSCRIPTION, headers=self.auth(token))

        users = self.client.app.state.users
        found = await users.subscriptions_for_device("any-device-whatsoever")
        self.assertEqual([s.endpoint for s in found], [SUBSCRIPTION["endpoint"]])

    async def test_a_scoped_user_is_only_reachable_for_their_own_devices(self):
        admin_token = await self.admin_token()
        await self.client.post(
            f"{BASE}/users",
            json={
                "username": "dispatcher",
                "password": "dispatch123",
                "role": "user",
                "devices": ["mine"],
            },
            headers=self.auth(admin_token),
        )
        login = await self.client.post(
            f"{BASE}/auth/login", json={"username": "dispatcher", "password": "dispatch123"}
        )
        user_token = login.json()["token"]
        await self.client.post(
            f"{BASE}/push/subscribe", json=SUBSCRIPTION, headers=self.auth(user_token)
        )

        users = self.client.app.state.users
        self.assertEqual(len(await users.subscriptions_for_device("mine")), 1)
        self.assertEqual(await users.subscriptions_for_device("not-mine"), [])

    async def test_resubscribing_the_same_endpoint_does_not_duplicate_it(self):
        token = await self.admin_token()
        for _ in range(2):
            await self.client.post(
                f"{BASE}/push/subscribe", json=SUBSCRIPTION, headers=self.auth(token)
            )
        users = self.client.app.state.users
        found = await users.subscriptions_for_device("whatever")
        self.assertEqual(len(found), 1)


class TestUnsubscribe(PushTestCase):
    async def test_removes_a_previously_stored_subscription(self):
        token = await self.admin_token()
        await self.client.post(f"{BASE}/push/subscribe", json=SUBSCRIPTION, headers=self.auth(token))

        response = await self.client.post(
            f"{BASE}/push/unsubscribe",
            json={"endpoint": SUBSCRIPTION["endpoint"]},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 204)

        users = self.client.app.state.users
        self.assertEqual(await users.subscriptions_for_device("whatever"), [])

    async def test_unsubscribing_something_never_subscribed_still_succeeds(self):
        token = await self.admin_token()
        response = await self.client.post(
            f"{BASE}/push/unsubscribe",
            json={"endpoint": "https://push.example.com/never-seen"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 204)

    async def test_requires_sign_in(self):
        response = await self.client.post(
            f"{BASE}/push/unsubscribe", json={"endpoint": SUBSCRIPTION["endpoint"]}
        )
        self.assertEqual(response.status_code, 401)
