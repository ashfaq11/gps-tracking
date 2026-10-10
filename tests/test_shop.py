"""The device shop: /shop/products and /shop/orders."""

import os
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
PRODUCT = {"name": "GT06 tracker", "description": "With relay", "price": 2499}
ADDRESS = {
    "contact_name": "Ravi Kumar",
    "phone": "+91 98450 12345",
    "address": "12, 4th Cross, Indiranagar",
    "city": "Bengaluru",
    "state": "Karnataka",
    "pincode": "560038",
}


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestShop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsgiClient(
            create_app(
                ApiConfig(
                    backend="memory",
                    bootstrap_admin_username=ADMIN["username"],
                    bootstrap_admin_password=ADMIN["password"],
                )
            )
        )
        await self.client.__aenter__()
        self.admin = await self.sign_in(ADMIN)
        self.ravi = await self.customer("ravi")

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def sign_in(self, credentials):
        login = await self.client.post(f"{BASE}/auth/login", json=credentials)
        return {"Authorization": f"Bearer {login.json()['token']}"}

    async def customer(self, username):
        account = {"username": username, "password": "password123"}
        created = await self.client.post(f"{BASE}/users", json=account, headers=self.admin)
        self.assertEqual(created.status_code, 201)
        return await self.sign_in(account)

    async def product(self, **overrides):
        response = await self.client.post(
            f"{BASE}/shop/products", json={**PRODUCT, **overrides}, headers=self.admin
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    async def order(self, headers, product_id, **overrides):
        return await self.client.post(
            f"{BASE}/shop/orders",
            json={"product_id": product_id, "quantity": 2, **ADDRESS, **overrides},
            headers=headers,
        )

    async def admin_update(self, order_id, **changes):
        return await self.client.patch(
            f"{BASE}/shop/orders/{order_id}", json=changes, headers=self.admin
        )

    # --- products ---

    async def test_only_an_admin_adds_products(self):
        response = await self.client.post(f"{BASE}/shop/products", json=PRODUCT, headers=self.ravi)
        self.assertEqual(response.status_code, 403)

    async def test_customers_see_only_products_on_sale(self):
        cheap = await self.product(name="Basic", price=1499)
        hidden = await self.product(name="Old model", price=999, active=False)
        mine = (await self.client.get(f"{BASE}/shop/products", headers=self.ravi)).json()
        self.assertEqual([p["id"] for p in mine], [cheap["id"]])
        everything = (await self.client.get(f"{BASE}/shop/products", headers=self.admin)).json()
        self.assertEqual([p["id"] for p in everything], [hidden["id"], cheap["id"]])

    async def test_a_price_change_leaves_placed_orders_alone(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        patched = await self.client.patch(
            f"{BASE}/shop/products/{product['id']}", json={"price": 2999}, headers=self.admin
        )
        self.assertEqual(patched.json()["price"], 2999)
        again = await self.client.get(f"{BASE}/shop/orders/{order['id']}", headers=self.ravi)
        self.assertEqual((again.json()["unit_price"], again.json()["total"]), (2499, 4998))

    # --- placing ---

    async def test_placing_an_order(self):
        product = await self.product()
        response = await self.order(self.ravi, product["id"])
        self.assertEqual(response.status_code, 201)
        order = response.json()
        self.assertEqual(order["status"], "placed")
        self.assertEqual(order["payment_method"], "cod")
        self.assertEqual(
            (order["product_name"], order["quantity"], order["total"]), ("GT06 tracker", 2, 4998)
        )
        self.assertEqual(order["username"], "ravi")
        self.assertEqual([e["status"] for e in order["events"]], ["placed"])

    async def test_a_hidden_or_missing_product_cannot_be_ordered(self):
        hidden = await self.product(active=False)
        self.assertEqual((await self.order(self.ravi, hidden["id"])).status_code, 404)
        self.assertEqual((await self.order(self.ravi, 999)).status_code, 404)

    async def test_the_address_is_validated(self):
        product = await self.product()
        self.assertEqual(
            (await self.order(self.ravi, product["id"], pincode="56")).status_code, 422
        )
        self.assertEqual((await self.order(self.ravi, product["id"], quantity=11)).status_code, 422)
        self.assertEqual(
            (await self.order(self.ravi, product["id"], phone="call me")).status_code, 422
        )

    async def test_signing_in_is_required(self):
        response = await self.client.get(f"{BASE}/shop/products")
        self.assertEqual(response.status_code, 401)

    # --- seeing ---

    async def test_a_customer_sees_only_their_own_orders(self):
        product = await self.product()
        mine = (await self.order(self.ravi, product["id"])).json()
        asha = await self.customer("asha")
        theirs = (await self.order(asha, product["id"])).json()

        listed = (await self.client.get(f"{BASE}/shop/orders", headers=self.ravi)).json()
        self.assertEqual([o["id"] for o in listed], [mine["id"]])
        # Not yours looks exactly like does not exist.
        other = await self.client.get(f"{BASE}/shop/orders/{theirs['id']}", headers=self.ravi)
        missing = await self.client.get(f"{BASE}/shop/orders/999", headers=self.ravi)
        self.assertEqual((other.status_code, missing.status_code), (404, 404))
        self.assertEqual(other.json()["detail"], f"No order {theirs['id']}")

        everyone = (await self.client.get(f"{BASE}/shop/orders", headers=self.admin)).json()
        self.assertEqual([o["id"] for o in everyone], [theirs["id"], mine["id"]])

    async def test_the_admin_note_stays_with_admins(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        await self.admin_update(order["id"], admin_note="Called, confirmed by phone")
        seen = (
            await self.client.get(f"{BASE}/shop/orders/{order['id']}", headers=self.ravi)
        ).json()
        self.assertIsNone(seen["admin_note"])
        admin_seen = await self.client.get(f"{BASE}/shop/orders/{order['id']}", headers=self.admin)
        self.assertEqual(admin_seen.json()["admin_note"], "Called, confirmed by phone")

    async def test_orders_filter_by_status(self):
        product = await self.product()
        first = (await self.order(self.ravi, product["id"])).json()
        await self.order(self.ravi, product["id"])
        await self.admin_update(first["id"], status="confirmed")
        confirmed = await self.client.get(
            f"{BASE}/shop/orders?status=confirmed", headers=self.admin
        )
        self.assertEqual([o["id"] for o in confirmed.json()], [first["id"]])

    # --- cancelling ---

    async def test_a_customer_cancels_until_it_is_confirmed(self):
        product = await self.product()
        early = (await self.order(self.ravi, product["id"])).json()
        cancelled = await self.client.post(
            f"{BASE}/shop/orders/{early['id']}/cancel", headers=self.ravi
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual([e["status"] for e in cancelled.json()["events"]], ["placed", "cancelled"])

        late = (await self.order(self.ravi, product["id"])).json()
        await self.admin_update(late["id"], status="confirmed")
        refused = await self.client.post(
            f"{BASE}/shop/orders/{late['id']}/cancel", headers=self.ravi
        )
        self.assertEqual(refused.status_code, 409)

    async def test_an_admin_cancels_until_it_is_delivered(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        await self.admin_update(order["id"], status="shipped")
        response = await self.client.post(
            f"{BASE}/shop/orders/{order['id']}/cancel", headers=self.admin
        )
        self.assertEqual(response.json()["status"], "cancelled")

    async def test_nobody_cancels_someone_elses_order(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        asha = await self.customer("asha")
        response = await self.client.post(f"{BASE}/shop/orders/{order['id']}/cancel", headers=asha)
        self.assertEqual(response.status_code, 404)

    # --- fulfilling ---

    async def test_only_an_admin_updates_an_order(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        response = await self.client.patch(
            f"{BASE}/shop/orders/{order['id']}", json={"status": "delivered"}, headers=self.ravi
        )
        self.assertEqual(response.status_code, 403)

    async def test_the_timeline_records_each_step_and_who_took_it(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        await self.admin_update(order["id"], status="confirmed")
        shipped = await self.admin_update(
            order["id"], status="shipped", tracking="DTDC D123456", note="Sent by DTDC"
        )
        body = shipped.json()
        self.assertEqual(body["tracking"], "DTDC D123456")
        self.assertEqual(
            [(e["status"], e["note"], e["changed_by"]) for e in body["events"]],
            [
                ("placed", None, "ravi"),
                ("confirmed", None, "admin"),
                ("shipped", "Sent by DTDC", "admin"),
            ],
        )
        # A change without a new status adds no step.
        noted = await self.admin_update(order["id"], tracking="DTDC D999")
        self.assertEqual(len(noted.json()["events"]), 3)

    async def test_a_final_status_does_not_change(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        await self.admin_update(order["id"], status="delivered")
        response = await self.admin_update(order["id"], status="placed")
        self.assertEqual(response.status_code, 409)
        # Other fields still can.
        self.assertEqual((await self.admin_update(order["id"], tracking="X1")).status_code, 200)

    async def test_shipped_trackers_are_added_to_the_customers_account(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        # Noted while confirmed: not claimed yet.
        await self.admin_update(order["id"], status="confirmed", device_ids=[IMEI])
        me = (await self.client.get(f"{BASE}/auth/me", headers=self.ravi)).json()
        self.assertEqual(me["devices"], [])

        shipped = await self.admin_update(order["id"], status="shipped")
        self.assertEqual(shipped.status_code, 200)
        me = (await self.client.get(f"{BASE}/auth/me", headers=self.ravi)).json()
        self.assertEqual(me["devices"], [IMEI])
        # Marking it delivered with the same IMEI again is harmless.
        delivered = await self.admin_update(
            order["id"], status="delivered", device_ids=[IMEI, IMEI]
        )
        self.assertEqual(delivered.status_code, 200)
        self.assertEqual(delivered.json()["device_ids"], [IMEI])

    async def test_an_imei_owned_by_someone_else_is_refused_and_nothing_changes(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        asha = await self.customer("asha")
        asha_order = (await self.order(asha, product["id"])).json()
        await self.admin_update(asha_order["id"], status="shipped", device_ids=[IMEI])

        other = "868120303370000"
        response = await self.admin_update(order["id"], status="shipped", device_ids=[other, IMEI])
        self.assertEqual(response.status_code, 409)
        self.assertIn(IMEI, response.json()["detail"])
        me = (await self.client.get(f"{BASE}/auth/me", headers=self.ravi)).json()
        self.assertEqual(me["devices"], [])
        unchanged = await self.client.get(f"{BASE}/shop/orders/{order['id']}", headers=self.admin)
        self.assertEqual(unchanged.json()["status"], "placed")

    async def test_imeis_must_be_digits(self):
        product = await self.product()
        order = (await self.order(self.ravi, product["id"])).json()
        response = await self.admin_update(order["id"], device_ids=["not-an-imei"])
        self.assertEqual(response.status_code, 422)


PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(FASTAPI_AVAILABLE and PG_DSN, "TEST_PG_DSN is not set")
class TestShopPostgres(TestShop):
    """The same rules against PostgresShopRepository's SQL. Needs a scratch
    database with sql/schema.sql applied; it empties the account and shop
    tables first."""

    async def asyncSetUp(self):
        import asyncpg

        conn = await asyncpg.connect(PG_DSN)
        try:
            await conn.execute(
                "TRUNCATE device_order_events, device_orders, device_products, "
                "device_claims, user_devices, user_sessions, users RESTART IDENTITY CASCADE"
            )
        finally:
            await conn.close()
        self.client = AsgiClient(
            create_app(
                ApiConfig(
                    backend="postgres",
                    pg_dsn=PG_DSN,
                    bootstrap_admin_username=ADMIN["username"],
                    bootstrap_admin_password=ADMIN["password"],
                )
            )
        )
        await self.client.__aenter__()
        self.admin = await self.sign_in(ADMIN)
        self.ravi = await self.customer("ravi")


if __name__ == "__main__":
    unittest.main()
