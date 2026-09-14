"""
Live location's fan-out logic.

The WebSocket handshake itself is not exercised here -- the test harness
(asgi_client.py) is plain HTTP request/response and has no upgrade support --
but the actual risk in this feature is scoping (does a broadcast reach only
the connections that should see a device), which is pure Python and needs no
transport at all to test.
"""

import unittest

from api.live import ConnectionManager


class FakeSocket:
    """Stands in for a WebSocket: records what it was sent."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class FailingSocket(FakeSocket):
    async def send_json(self, payload: dict) -> None:
        raise ConnectionResetError("gone")


class TestConnectionManager(unittest.IsolatedAsyncioTestCase):
    async def test_an_admin_socket_receives_every_device(self):
        manager = ConnectionManager()
        admin = FakeSocket()
        await manager.register(admin, devices=None)

        await manager.broadcast({"device_id": "a"})
        await manager.broadcast({"device_id": "b"})

        self.assertEqual([m["device_id"] for m in admin.sent], ["a", "b"])

    async def test_a_scoped_socket_receives_only_its_own_devices(self):
        manager = ConnectionManager()
        scoped = FakeSocket()
        await manager.register(scoped, devices=frozenset({"a"}))

        await manager.broadcast({"device_id": "a"})
        await manager.broadcast({"device_id": "b"})

        self.assertEqual([m["device_id"] for m in scoped.sent], ["a"])

    async def test_a_socket_with_no_devices_receives_nothing(self):
        manager = ConnectionManager()
        empty = FakeSocket()
        await manager.register(empty, devices=frozenset())

        await manager.broadcast({"device_id": "a"})

        self.assertEqual(empty.sent, [])

    async def test_unregistering_stops_further_delivery(self):
        manager = ConnectionManager()
        socket = FakeSocket()
        await manager.register(socket, devices=None)
        await manager.unregister(socket)

        await manager.broadcast({"device_id": "a"})

        self.assertEqual(socket.sent, [])
        self.assertEqual(manager.count, 0)

    async def test_a_malformed_event_with_no_device_id_is_dropped(self):
        manager = ConnectionManager()
        admin = FakeSocket()
        await manager.register(admin, devices=None)

        await manager.broadcast({"speed_kmh": 10})

        self.assertEqual(admin.sent, [])

    async def test_a_failing_socket_does_not_block_delivery_to_the_others(self):
        manager = ConnectionManager()
        dead = FailingSocket()
        alive = FakeSocket()
        await manager.register(dead, devices=None)
        await manager.register(alive, devices=None)

        await manager.broadcast({"device_id": "a"})

        self.assertEqual([m["device_id"] for m in alive.sent], ["a"])

    async def test_count_reflects_registrations(self):
        manager = ConnectionManager()
        self.assertEqual(manager.count, 0)
        a, b = FakeSocket(), FakeSocket()
        await manager.register(a, devices=None)
        await manager.register(b, devices=None)
        self.assertEqual(manager.count, 2)
        await manager.unregister(a)
        self.assertEqual(manager.count, 1)


if __name__ == "__main__":
    unittest.main()
