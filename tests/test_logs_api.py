"""GET/PUT /logs: the weekly log files and the log level, for admins only."""

import logging
import os
import tempfile
import unittest

from weekly_logs import read_level

try:
    from .test_api import ADMIN, BASE, ApiTestCase

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False

WEEK = "19102026_25102026.log"
CONTENT = (
    "2026-10-22 10:00:00 IST ERROR   [api.error] [abc123abc123] it broke\n"
    "Traceback (most recent call last):\n"
    "  File \"x.py\", line 1\n"
    "2026-10-22 10:00:05 IST WARNING [gps_gateway.session] odd packet\n"
    "2026-10-22 10:00:09 IST ERROR [gps_gateway.sink] write failed\n"
)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestLogs(ApiTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        with open(os.path.join(self.dir, WEEK), "w", encoding="utf-8") as f:
            f.write(CONTENT)
        self.saved_level = logging.getLogger().level
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        logging.getLogger().setLevel(self.saved_level)
        self.tmp.cleanup()

    def make_app(self, **overrides):
        return super().make_app(log_dir=self.dir, **overrides)

    async def test_lists_the_week_files(self):
        response = await self.client.get(f"{BASE}/logs", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        (week,) = response.json()
        self.assertEqual(week["name"], WEEK)
        self.assertEqual((week["week_start"], week["week_end"]), ("2026-10-19", "2026-10-25"))
        self.assertEqual(week["size_bytes"], len(CONTENT))

    async def test_reads_a_file_whole_its_tail_or_one_level(self):
        whole = await self.client.get(f"{BASE}/logs/{WEEK}", headers=self.auth())
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(whole.body.decode(), CONTENT)
        tail = await self.client.get(f"{BASE}/logs/{WEEK}?tail=1", headers=self.auth())
        self.assertEqual(tail.body.decode(), "2026-10-22 10:00:09 IST ERROR [gps_gateway.sink] write failed\n")
        errors = await self.client.get(f"{BASE}/logs/{WEEK}?level=ERROR", headers=self.auth())
        self.assertIn("Traceback", errors.body.decode())  # the record keeps its traceback
        self.assertNotIn("odd packet", errors.body.decode())
        self.assertIn("write failed", errors.body.decode())

    async def test_entries_newest_first_a_page_at_a_time_in_utc(self):
        response = await self.client.get(f"{BASE}/logs/{WEEK}/entries?size=2", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.body)
        page = response.json()
        self.assertEqual((page["total"], page["pages"], page["page"]), (3, 2, 1))
        newest, middle = page["entries"]
        self.assertEqual(newest["message"], "write failed")
        self.assertEqual((newest["source"], newest["logger"]), ("gateway", "gps_gateway.sink"))
        self.assertEqual(newest["at"], "2026-10-22T04:30:09Z")  # 10:00:09 IST, sent as UTC
        self.assertEqual(middle["level"], "WARNING")
        second = (await self.client.get(f"{BASE}/logs/{WEEK}/entries?size=2&page=2", headers=self.auth())).json()
        (oldest,) = second["entries"]
        self.assertEqual((oldest["source"], oldest["request_id"]), ("api", "abc123abc123"))
        self.assertIn("Traceback", oldest["details"])

    async def test_entries_filter_by_level_and_text(self):
        errors = (await self.client.get(f"{BASE}/logs/{WEEK}/entries?level=ERROR", headers=self.auth())).json()
        self.assertEqual([e["message"] for e in errors["entries"]], ["write failed", "it broke"])
        found = (await self.client.get(f"{BASE}/logs/{WEEK}/entries?q=X.PY", headers=self.auth())).json()
        self.assertEqual([e["message"] for e in found["entries"]], ["it broke"])  # matched in its traceback
        rid = (await self.client.get(f"{BASE}/logs/{WEEK}/entries?q=abc123abc123", headers=self.auth())).json()
        self.assertEqual(rid["total"], 1)
        missing = await self.client.get(f"{BASE}/logs/nope.log/entries", headers=self.auth())
        self.assertEqual(missing.status_code, 404)

    async def test_nothing_outside_the_week_files_can_be_read(self):
        for name in ["log-level", "..%2F..%2Fetc%2Fpasswd", "26102026_01112026.log"]:
            response = await self.client.get(f"{BASE}/logs/{name}", headers=self.auth())
            self.assertEqual(response.status_code, 404, name)

    async def test_the_level_is_error_until_changed_and_then_kept(self):
        now = await self.client.get(f"{BASE}/logs/level", headers=self.auth())
        self.assertEqual(now.json(), {"level": "ERROR", "saved": False})
        changed = await self.client.put(f"{BASE}/logs/level", json={"level": "WARNING"}, headers=self.auth())
        self.assertEqual(changed.json(), {"level": "WARNING", "saved": True})
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(read_level(self.dir, "ERROR"), "WARNING")  # what the gateway picks up
        bad = await self.client.put(f"{BASE}/logs/level", json={"level": "LOUD"}, headers=self.auth())
        self.assertEqual(bad.status_code, 422)

    async def test_admins_only(self):
        created = await self.client.post(
            f"{BASE}/users",
            json={"username": "ravi", "password": "Ravi-password1", "role": "user"},
            headers=self.auth(),
        )
        self.assertEqual(created.status_code, 201, created.body.decode())
        login = await self.client.post(f"{BASE}/auth/login", json={"username": "ravi", "password": "Ravi-password1"})
        user = {"Authorization": f"Bearer {login.json()['token']}"}
        for method, path in [("get", "/logs"), ("get", f"/logs/{WEEK}"), ("get", "/logs/level")]:
            response = await getattr(self.client, method)(f"{BASE}{path}", headers=user)
            self.assertEqual(response.status_code, 403, path)
        self.assertEqual((await self.client.get(f"{BASE}/logs")).status_code, 401)
        self.assertNotEqual(ADMIN["username"], "ravi")


if __name__ == "__main__":
    unittest.main()
