"""Stored and sent as UTC, shown in IST (api/timeutil.py)."""

import unittest
from datetime import datetime, timedelta, timezone

try:
    from api.schemas import LocationIn
    from api.timeutil import IST, as_utc, ist_clock

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestUtc(unittest.TestCase):
    def test_a_naive_time_is_utc_and_an_offset_is_converted(self):
        self.assertEqual(as_utc(datetime(2026, 10, 22, 5, 0)), datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))
        ist = datetime(2026, 10, 22, 10, 30, tzinfo=IST)
        self.assertEqual(as_utc(ist), datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(as_utc(ist).utcoffset(), timedelta(0))
        self.assertIsNone(as_utc(None))

    def test_a_fix_sent_in_ist_is_kept_and_sent_back_as_utc(self):
        fix = LocationIn(device_id="d", latitude=1, longitude=2, fixed_at="2026-10-22T10:30:00+05:30")
        self.assertEqual(fix.fixed_at, datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc))
        self.assertIn('"fixed_at":"2026-10-22T05:00:00Z"', fix.model_dump_json())

    def test_people_see_ist(self):
        self.assertEqual(ist_clock("2026-10-22T05:12:00Z"), "10:42 AM IST")
        self.assertEqual(ist_clock(datetime(2026, 10, 22, 18, 5, tzinfo=timezone.utc)), "11:35 PM IST")
        self.assertIsNone(ist_clock("junk"))


if __name__ == "__main__":
    unittest.main()
