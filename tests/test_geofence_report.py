"""Geofence report rules (api/geofence_report.py) and GET /geofences/report."""

import unittest
from datetime import datetime, timedelta, timezone

from api.geofence_report import summarize_geofences
from api.schemas import GeofenceEventOut, GeofenceOut

from .test_geofences import CIRCLE, SQUARE, GeofenceTestCase

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
SINCE, UNTIL = T0, T0 + timedelta(hours=10)
BASE = "/api/v1"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def fence(fence_id: int = 1, name: str = "Depot") -> GeofenceOut:
    return GeofenceOut(
        id=fence_id, name=name, kind="circle", center={"lat": 0, "lng": 0}, radius_m=100,
        vertices=None, device_ids=["dev-a"], alert_on_exit=True, alert_on_enter=False,
        owner_id=1, owner_username=None, created_at=T0, updated_at=T0,
    )


_next_id = iter(range(1, 10_000))


def event(kind: str, minute: float, device: str = "dev-a", fence_id: int = 1) -> GeofenceEventOut:
    return GeofenceEventOut(
        id=next(_next_id), geofence_id=fence_id, geofence_name="Depot", device_id=device,
        kind=kind, latitude=0, longitude=0, occurred_at=at(minute), alerted=kind == "exit",
    )


def report(events, fences=None, now=UNTIL):
    return summarize_geofences(fences or [fence()], events, SINCE, UNTIL, now)


class TestTimeInside(unittest.TestCase):
    def test_an_enter_and_its_exit_make_one_visit(self):
        [row] = report([event("enter", 60), event("exit", 150)])
        self.assertEqual((row.entries, row.exits, row.alerts), (1, 1, 1))
        self.assertAlmostEqual(row.seconds_inside / 60, 90)

    def test_already_inside_at_the_start_counts_from_the_start(self):
        [row] = report([event("exit", 45)])
        self.assertAlmostEqual(row.seconds_inside / 60, 45)

    def test_still_inside_at_the_end_counts_to_the_end(self):
        [row] = report([event("enter", 540)])  # window ends at 600
        self.assertAlmostEqual(row.seconds_inside / 60, 60)

    def test_a_window_reaching_into_the_future_counts_only_to_now(self):
        [row] = report([event("enter", 60)], now=at(90))
        self.assertAlmostEqual(row.seconds_inside / 60, 30)

    def test_a_repeated_enter_or_exit_adds_nothing(self):
        [row] = report([event("enter", 0), event("enter", 30), event("exit", 60), event("exit", 90)])
        self.assertEqual((row.entries, row.exits), (2, 2))
        self.assertAlmostEqual(row.seconds_inside / 60, 60)

    def test_vehicles_are_kept_apart_and_summed_per_fence(self):
        [row] = report([
            event("enter", 0, "dev-a"), event("enter", 10, "dev-b"),
            event("exit", 20, "dev-a"), event("exit", 70, "dev-b"),
        ])
        self.assertEqual(sorted(row.vehicles), ["dev-a", "dev-b"])
        self.assertAlmostEqual(row.vehicles["dev-a"].seconds_inside / 60, 20)
        self.assertAlmostEqual(row.seconds_inside / 60, 80)
        self.assertEqual(row.last_event_at, at(70))

    def test_crossings_outside_the_window_do_not_count(self):
        [row] = report([event("enter", -30), event("exit", 700)])
        self.assertEqual((row.entries, row.exits, row.seconds_inside), (0, 0, 0))

    def test_quiet_fences_are_listed_after_busy_ones(self):
        rows = report([event("enter", 5, fence_id=2)], fences=[fence(1, "Alpha"), fence(2, "Zulu")])
        self.assertEqual([r.name for r in rows], ["Zulu", "Alpha"])
        self.assertEqual(rows[1].entries + rows[1].exits, 0)


class TestReportEndpoint(GeofenceTestCase):
    async def fetch(self, token=None, query=""):
        return await self.client.get(
            f"{BASE}/geofences/report{query}", headers=self.auth(token or self.admin)
        )

    async def test_reports_crossings_vehicles_and_time_inside(self):
        depot = (await self.create(CIRCLE)).json()
        await self.create(SQUARE)
        repo = self.app.state.geofences
        now = datetime.now(timezone.utc)
        repo.record_event(depot["id"], "dev-a", "enter", occurred_at=now - timedelta(minutes=50))
        repo.record_event(depot["id"], "dev-a", "exit", occurred_at=now - timedelta(minutes=20))

        response = await self.fetch()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totals"], {"geofences": 1, "entries": 1, "exits": 1, "alerts": 1, "vehicles": 1})
        self.assertFalse(body["truncated"])
        first, second = body["geofences"]
        self.assertEqual((first["name"], second["name"]), ("Depot", "Yard"))
        self.assertAlmostEqual(first["time_inside_minutes"], 30, places=3)
        self.assertEqual(first["vehicles"][0]["device_id"], "dev-a")
        self.assertEqual((second["entries"], second["vehicles"]), (0, []))

    async def test_a_user_sees_only_their_own_geofences_and_vehicles(self):
        token = await self.user_token("owner", ["dev-a"])
        mine = (await self.create({**CIRCLE, "name": "Mine"}, token)).json()
        await self.create({**SQUARE, "name": "Theirs"})
        self.app.state.geofences.record_event(mine["id"], "dev-a", "enter")

        body = (await self.fetch(token)).json()
        self.assertEqual([g["name"] for g in body["geofences"]], ["Mine"])
        self.assertEqual(body["totals"]["entries"], 1)

    async def test_rejects_a_backwards_or_oversized_window(self):
        backwards = await self.fetch(query="?since=2026-09-02T00:00:00Z&until=2026-09-01T00:00:00Z")
        too_long = await self.fetch(query="?since=2026-01-01T00:00:00Z&until=2026-03-01T00:00:00Z")
        self.assertEqual((backwards.status_code, too_long.status_code), (422, 422))

    async def test_requires_sign_in(self):
        self.assertEqual((await self.client.get(f"{BASE}/geofences/report")).status_code, 401)

    async def test_the_events_endpoint_filters_to_a_day(self):
        depot = (await self.create(CIRCLE)).json()
        repo = self.app.state.geofences
        day = datetime(2026, 9, 20, tzinfo=timezone.utc)
        repo.record_event(depot["id"], "dev-a", "enter", occurred_at=day + timedelta(hours=9))
        repo.record_event(depot["id"], "dev-a", "exit", occurred_at=day + timedelta(hours=17))
        repo.record_event(depot["id"], "dev-a", "enter", occurred_at=day + timedelta(days=1, hours=9))
        events = (
            await self.client.get(
                f"{BASE}/geofences/events?since=2026-09-20T00:00:00Z&until=2026-09-20T23:59:59Z",
                headers=self.auth(self.admin),
            )
        ).json()
        self.assertEqual([e["kind"] for e in events], ["exit", "enter"])

    async def test_the_events_list_takes_the_same_window(self):
        depot = (await self.create(CIRCLE)).json()
        repo = self.app.state.geofences
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        repo.record_event(depot["id"], "dev-a", "enter", occurred_at=old)
        repo.record_event(depot["id"], "dev-a", "exit")
        events = await repo.list_events(
            owner_id=None, device_scope=None, since=old + timedelta(days=1)
        )
        self.assertEqual([e.kind for e in events], ["exit"])


if __name__ == "__main__":
    unittest.main()
