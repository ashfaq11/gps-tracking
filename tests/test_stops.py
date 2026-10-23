"""The stop watcher: it calls announce_due_stops() on its interval, keeps going
after a failure, and stops when cancelled. The function itself is SQL, checked
in tests/test_alerts_postgres.py."""

import asyncio
import unittest

from api.stops import run_stop_watcher


class FakePool:
    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    async def fetchval(self, sql):
        self.calls += 1
        assert "announce_due_stops" in sql
        if self.fail_first and self.calls == 1:
            raise OSError("storage went away")
        return 0


class TestStopWatcher(unittest.IsolatedAsyncioTestCase):
    async def run_for(self, pool, seconds):
        task = asyncio.create_task(run_stop_watcher(pool, 0.01))
        await asyncio.sleep(seconds)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_it_calls_the_function_again_and_again(self):
        pool = FakePool()
        await self.run_for(pool, 0.1)
        self.assertGreater(pool.calls, 3)

    async def test_a_failure_does_not_end_it(self):
        pool = FakePool(fail_first=True)
        await self.run_for(pool, 0.1)
        self.assertGreater(pool.calls, 2)


if __name__ == "__main__":
    unittest.main()
