"""Dashboard accounts: sign-in, admin-only management, and deactivation."""

import unittest

try:
    from api.config import ApiConfig
    from api.main import create_app

    from .asgi_client import AsgiClient

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

BASE = "/api/v1"
ADMIN = {"username": "admin", "password": "Admin-password1"}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class AccountTestCase(unittest.IsolatedAsyncioTestCase):
    def make_app(self, **overrides):
        config = ApiConfig(
            backend="memory",
            bootstrap_admin_username=ADMIN["username"],
            bootstrap_admin_password=ADMIN["password"],
            **overrides,
        )
        return create_app(config)

    async def asyncSetUp(self):
        self.client = AsgiClient(self.make_app())
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def login(self, username=ADMIN["username"], password=ADMIN["password"]):
        return await self.client.post(
            f"{BASE}/auth/login", json={"username": username, "password": password}
        )

    async def admin_token(self):
        return (await self.login()).json()["token"]

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    async def create_user(self, token, **overrides):
        payload = {"username": "dispatcher", "password": "Dispatch-123", "role": "user"}
        payload.update(overrides)
        return await self.client.post(f"{BASE}/users", json=payload, headers=self.auth(token))


class TestBootstrap(AccountTestCase):
    async def test_the_first_admin_is_seeded_so_a_new_database_is_reachable(self):
        response = await self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "admin")

    async def test_no_bootstrap_admin_means_no_accounts_at_all(self):
        client = AsgiClient(create_app(ApiConfig(backend="memory")))
        async with client:
            response = await client.post(
                f"{BASE}/auth/login", json={"username": "admin", "password": "Admin-password1"}
            )
            self.assertEqual(response.status_code, 401)


class TestLogin(AccountTestCase):
    async def test_a_wrong_password_is_rejected(self):
        self.assertEqual((await self.login(password="wrong")).status_code, 401)

    async def test_an_unknown_username_is_rejected(self):
        self.assertEqual((await self.login(username="nobody")).status_code, 401)

    async def test_a_username_is_matched_regardless_of_case(self):
        self.assertEqual((await self.login(username="ADMIN")).status_code, 200)

    async def test_the_response_never_carries_a_password_hash(self):
        body = (await self.login()).json()
        self.assertNotIn("password_hash", body["user"])
        self.assertNotIn("password", body["user"])

    async def test_logout_makes_the_token_unusable(self):
        token = await self.admin_token()
        self.assertEqual(
            (await self.client.post(f"{BASE}/auth/logout", headers=self.auth(token))).status_code,
            204,
        )
        self.assertEqual(
            (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).status_code, 401
        )

    async def test_a_made_up_token_is_rejected(self):
        response = await self.client.get(f"{BASE}/auth/me", headers=self.auth("not-a-real-token"))
        self.assertEqual(response.status_code, 401)


class TestAdminOnly(AccountTestCase):
    async def test_listing_users_needs_a_token(self):
        self.assertEqual((await self.client.get(f"{BASE}/users")).status_code, 401)

    async def test_a_non_admin_cannot_list_users(self):
        admin = await self.admin_token()
        await self.create_user(admin)
        token = (await self.login("dispatcher", "Dispatch-123")).json()["token"]

        response = await self.client.get(f"{BASE}/users", headers=self.auth(token))
        # 403 not 401: signing in again would not help, and the client needs
        # that distinction to choose between a login form and "not allowed".
        self.assertEqual(response.status_code, 403)

    async def test_a_non_admin_cannot_create_a_user(self):
        admin = await self.admin_token()
        await self.create_user(admin)
        token = (await self.login("dispatcher", "Dispatch-123")).json()["token"]

        response = await self.create_user(token, username="sneaky")
        self.assertEqual(response.status_code, 403)


class TestCreateAndEdit(AccountTestCase):
    async def test_creating_a_user_stores_its_device_assignments(self):
        token = await self.admin_token()
        response = await self.create_user(token, devices=["dev-a", "dev-b"])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["devices"], ["dev-a", "dev-b"])

    async def test_a_duplicate_username_is_refused_regardless_of_case(self):
        token = await self.admin_token()
        await self.create_user(token)
        response = await self.create_user(token, username="Dispatcher")
        self.assertEqual(response.status_code, 409)

    async def test_a_short_password_is_refused(self):
        token = await self.admin_token()
        response = await self.create_user(token, password="short")
        self.assertEqual(response.status_code, 422)

    async def test_a_password_without_the_usual_mix_is_refused(self):
        token = await self.admin_token()
        # Long enough, but each lacks one of: upper, lower, digit, symbol.
        for weak in ("all-lower-case-1", "ALL-UPPER-CASE-1", "No-digits-here", "NoSymbols123"):
            response = await self.create_user(token, password=weak)
            self.assertEqual(response.status_code, 422, weak)
            self.assertIn("upper-case letter", str(response.json()["detail"]), weak)
        self.assertEqual((await self.create_user(token, password="Strong-pass1")).status_code, 201)

    async def test_an_older_weaker_password_still_signs_in(self):
        # The rule applies when a password is set, not at sign-in: an account
        # made before it (here, the bootstrap admin) keeps working.
        app = create_app(
            ApiConfig(
                backend="memory",
                bootstrap_admin_username=ADMIN["username"],
                bootstrap_admin_password="weakbutold",
            )
        )
        async with AsgiClient(app) as client:
            response = await client.post(
                f"{BASE}/auth/login", json={"username": ADMIN["username"], "password": "weakbutold"}
            )
        self.assertEqual(response.status_code, 200)

    async def test_an_admin_is_given_no_device_rows(self):
        # An admin sees every device, so per-device assignments would be
        # noise that later reads as a restriction.
        token = await self.admin_token()
        response = await self.create_user(
            token, username="second", role="admin", devices=["dev-a"]
        )
        self.assertEqual(response.json()["devices"], [])

    async def test_editing_replaces_only_the_fields_sent(self):
        token = await self.admin_token()
        user_id = (await self.create_user(token, full_name="Original")).json()["id"]

        response = await self.client.patch(
            f"{BASE}/users/{user_id}", json={"devices": ["dev-c"]}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["full_name"], "Original")
        self.assertEqual(response.json()["devices"], ["dev-c"])

    async def test_editing_an_unknown_account_is_a_404(self):
        token = await self.admin_token()
        response = await self.client.patch(
            f"{BASE}/users/9999", json={"full_name": "Ghost"}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 404)


class TestDeactivation(AccountTestCase):
    async def test_deactivating_invalidates_a_token_already_issued(self):
        admin = await self.admin_token()
        user_id = (await self.create_user(admin)).json()["id"]
        token = (await self.login("dispatcher", "Dispatch-123")).json()["token"]
        self.assertEqual(
            (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).status_code, 200
        )

        await self.client.patch(
            f"{BASE}/users/{user_id}", json={"is_active": False}, headers=self.auth(admin)
        )

        # The whole point of storing tokens rather than signing them: this is
        # 401 immediately, not whenever the token would have expired.
        self.assertEqual(
            (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).status_code, 401
        )

    async def test_a_deactivated_account_cannot_sign_in(self):
        admin = await self.admin_token()
        user_id = (await self.create_user(admin)).json()["id"]
        await self.client.patch(
            f"{BASE}/users/{user_id}", json={"is_active": False}, headers=self.auth(admin)
        )

        self.assertEqual((await self.login("dispatcher", "Dispatch-123")).status_code, 401)

    async def test_reactivating_restores_sign_in(self):
        admin = await self.admin_token()
        user_id = (await self.create_user(admin)).json()["id"]
        await self.client.patch(
            f"{BASE}/users/{user_id}", json={"is_active": False}, headers=self.auth(admin)
        )
        await self.client.patch(
            f"{BASE}/users/{user_id}", json={"is_active": True}, headers=self.auth(admin)
        )

        self.assertEqual((await self.login("dispatcher", "Dispatch-123")).status_code, 200)

    async def test_an_admin_cannot_deactivate_themselves(self):
        token = await self.admin_token()
        me = (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).json()

        response = await self.client.patch(
            f"{BASE}/users/{me['id']}", json={"is_active": False}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 400)

    async def test_the_last_active_admin_cannot_be_demoted(self):
        token = await self.admin_token()
        me = (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).json()

        response = await self.client.patch(
            f"{BASE}/users/{me['id']}", json={"role": "user"}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 400)

    async def test_changing_a_password_signs_the_account_out(self):
        admin = await self.admin_token()
        user_id = (await self.create_user(admin)).json()["id"]
        token = (await self.login("dispatcher", "Dispatch-123")).json()["token"]

        await self.client.patch(
            f"{BASE}/users/{user_id}", json={"password": "Brand-new123"}, headers=self.auth(admin)
        )

        self.assertEqual(
            (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).status_code, 401
        )
        self.assertEqual((await self.login("dispatcher", "Brand-new123")).status_code, 200)


class TestSelfService(AccountTestCase):
    """PATCH /auth/me -- an account editing itself."""

    async def _signed_up_token(self) -> str:
        await self.create_user(await self.admin_token())
        return (await self.login("dispatcher", "Dispatch-123")).json()["token"]

    async def test_can_update_its_own_full_name_and_contact_details(self):
        token = await self._signed_up_token()
        response = await self.client.patch(
            f"{BASE}/auth/me",
            json={"full_name": "Dee Patcher", "email": "dee@example.com", "mobile": "+911234567890"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["full_name"], "Dee Patcher")
        self.assertEqual(body["email"], "dee@example.com")
        self.assertEqual(body["mobile"], "+911234567890")

    async def test_cannot_promote_or_reactivate_or_reassign_itself(self):
        # SelfUpdate has no role/is_active/devices fields at all -- sending
        # them is simply ignored by the model, not rejected, same as any
        # unknown field.
        token = await self._signed_up_token()
        response = await self.client.patch(
            f"{BASE}/auth/me",
            json={"role": "admin", "is_active": False, "devices": ["some-device"]},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(response.json()["is_active"], True)

    async def test_changing_password_requires_the_current_one(self):
        token = await self._signed_up_token()
        response = await self.client.patch(
            f"{BASE}/auth/me", json={"password": "Brand-new123"}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 403)

    async def test_changing_password_rejects_a_wrong_current_password(self):
        token = await self._signed_up_token()
        response = await self.client.patch(
            f"{BASE}/auth/me",
            json={"current_password": "wrongone", "password": "Brand-new123"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 403)

    async def test_changing_password_with_the_right_current_password_works_and_signs_out(self):
        token = await self._signed_up_token()
        response = await self.client.patch(
            f"{BASE}/auth/me",
            json={"current_password": "Dispatch-123", "password": "Brand-new123"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 200)

        # This session -- the one that made the change -- is signed out too.
        self.assertEqual(
            (await self.client.get(f"{BASE}/auth/me", headers=self.auth(token))).status_code, 401
        )
        self.assertEqual((await self.login("dispatcher", "Brand-new123")).status_code, 200)

    async def test_taking_someone_elses_email_conflicts(self):
        admin = await self.admin_token()
        await self.create_user(admin, username="first", email="first@example.com")
        token = await self._signed_up_token()

        response = await self.client.patch(
            f"{BASE}/auth/me", json={"email": "first@example.com"}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 409)

    async def test_keeping_your_own_email_is_not_a_conflict(self):
        admin = await self.admin_token()
        await self.create_user(admin, email="dispatch@example.com")
        token = (await self.login("dispatcher", "Dispatch-123")).json()["token"]

        response = await self.client.patch(
            f"{BASE}/auth/me",
            json={"email": "dispatch@example.com", "full_name": "Dee"},
            headers=self.auth(token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["full_name"], "Dee")


if __name__ == "__main__":
    unittest.main()


class TestLoginThrottle(AccountTestCase):
    async def test_repeated_failures_are_throttled_with_retry_after(self):
        for _ in range(10):
            self.assertEqual((await self.login(password="wrong")).status_code, 401)
        blocked = await self.login(password="wrong")
        self.assertEqual(blocked.status_code, 429)
        self.assertGreater(int(blocked.headers["retry-after"]), 0)
        # Even the right password waits: the window has to slide first.
        self.assertEqual((await self.login()).status_code, 429)

    async def test_a_success_resets_the_count_for_that_source(self):
        for _ in range(9):
            await self.login(password="wrong")
        self.assertEqual((await self.login()).status_code, 200)
        for _ in range(9):
            self.assertEqual((await self.login(password="wrong")).status_code, 401)

    async def test_another_account_is_not_affected(self):
        for _ in range(11):
            await self.login(password="wrong")
        self.assertEqual((await self.login(username="nobody", password="x")).status_code, 401)


class TestThrottleWindow(unittest.TestCase):
    def test_the_window_slides(self):
        from api.throttle import LoginThrottle

        now = [0.0]
        throttle = LoginThrottle(per_source=2, per_username=5, window_seconds=60, clock=lambda: now[0])
        throttle.failed("Bob", "1.1.1.1")
        throttle.failed("bob", "1.1.1.1")
        self.assertEqual(throttle.retry_after("bob", "1.1.1.1"), 61)
        self.assertEqual(throttle.retry_after("bob", "2.2.2.2"), 0)
        now[0] = 61
        self.assertEqual(throttle.retry_after("bob", "1.1.1.1"), 0)


class TestCompression(AccountTestCase):
    async def test_large_responses_are_gzipped_for_clients_that_accept_it(self):
        response = await self.client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-encoding"), "gzip")

    async def test_clients_that_do_not_ask_get_plain_json(self):
        response = await self.client.get("/openapi.json")
        self.assertIsNone(response.headers.get("content-encoding"))
