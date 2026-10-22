"""weekly_logs.py: one shared file per week, outside the project, warnings and errors only."""

import logging
import os
import tempfile
import unittest
from datetime import date, datetime

from weekly_logs import (
    IST,
    LevelWatcher,
    WeeklyFileHandler,
    list_logs,
    log_path,
    prune,
    read_level,
    retention_weeks,
    week_file,
    week_start,
    write_level,
)


def record(level: int, message: str, when: datetime) -> logging.LogRecord:
    rec = logging.LogRecord("api.test", level, __file__, 1, message, None, None)
    rec.created = when.timestamp()
    return rec


class TestNames(unittest.TestCase):
    def test_a_week_is_monday_to_sunday_ist_named_ddmmyyyy(self):
        # Sunday 25 Oct 23:59 IST is still the week of Monday 19 Oct.
        self.assertEqual(week_start(datetime(2026, 10, 25, 23, 59, tzinfo=IST)), date(2026, 10, 19))
        self.assertEqual(week_start(datetime(2026, 10, 26, 0, 0, tzinfo=IST)), date(2026, 10, 26))
        self.assertEqual(week_file(date(2026, 10, 19)), "19102026_25102026.log")
        self.assertEqual(week_file(date(2026, 10, 26), "packets"), "26102026_01112026.packets.log")

    def test_retention_is_between_1_and_12_weeks(self):
        self.assertEqual(retention_weeks(None), 4)
        self.assertEqual(retention_weeks("0"), 1)
        self.assertEqual(retention_weeks("99"), 12)
        self.assertEqual(retention_weeks("junk"), 4)


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name: str) -> str:
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_writes_warnings_and_errors_to_the_weeks_file_never_info(self):
        handler = WeeklyFileHandler(self.dir, 4)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        when = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
        for level, text in [(logging.INFO, "chatty"), (logging.WARNING, "careful"), (logging.ERROR, "broken")]:
            handler.handle(record(level, text, when))
        handler.close()
        self.assertEqual(self.read("19102026_25102026.log"), "WARNING careful\nERROR broken\n")

    def test_a_new_week_starts_a_new_file(self):
        handler = WeeklyFileHandler(self.dir, 4)
        handler.handle(record(logging.ERROR, "sunday", datetime(2026, 10, 25, 23, 0, tzinfo=IST)))
        handler.handle(record(logging.ERROR, "monday", datetime(2026, 10, 26, 1, 0, tzinfo=IST)))
        handler.close()
        self.assertIn("sunday", self.read("19102026_25102026.log"))
        self.assertIn("monday", self.read("26102026_01112026.log"))

    def test_the_api_and_the_gateway_share_one_file(self):
        when = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
        api, gateway = WeeklyFileHandler(self.dir, 4), WeeklyFileHandler(self.dir, 4)
        api.handle(record(logging.ERROR, "from the api", when))
        gateway.handle(record(logging.ERROR, "from the gateway", when))
        api.handle(record(logging.ERROR, "the api again", when))
        api.close()
        gateway.close()
        self.assertEqual(
            self.read("19102026_25102026.log"), "from the api\nfrom the gateway\nthe api again\n"
        )

    def test_old_weeks_are_deleted_and_other_files_left_alone(self):
        for name in ["21092026_27092026.log", "28092026_04102026.packets.log",
                     "05102026_11102026.log", "19102026_25102026.log", "notes.txt", "log-level"]:
            open(os.path.join(self.dir, name), "w").close()
        prune(self.dir, 3, current=date(2026, 10, 19))
        self.assertEqual(
            sorted(os.listdir(self.dir)),
            ["05102026_11102026.log", "19102026_25102026.log", "log-level", "notes.txt"],
        )


class TestLevelAndListing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.saved = logging.getLogger().level

    def tearDown(self):
        logging.getLogger().setLevel(self.saved)
        self.tmp.cleanup()

    def test_the_saved_level_wins_over_the_default_and_nonsense_is_ignored(self):
        self.assertEqual(read_level(self.dir, "ERROR"), "ERROR")
        write_level(self.dir, "WARNING")
        self.assertEqual(read_level(self.dir, "ERROR"), "WARNING")
        write_level(self.dir, "loud")
        self.assertEqual(read_level(self.dir, "ERROR"), "ERROR")

    def test_the_watcher_follows_a_change(self):
        watcher = LevelWatcher(self.dir, "ERROR")
        self.assertFalse(watcher.apply())
        write_level(self.dir, "WARNING")
        self.assertTrue(watcher.apply())
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_lists_newest_week_first_and_never_resolves_outside_the_folder(self):
        for name in ["12102026_18102026.log", "19102026_25102026.log", "19102026_25102026.packets.log"]:
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("x")
        names = [f["name"] for f in list_logs(self.dir)]
        self.assertEqual(names, ["19102026_25102026.log", "19102026_25102026.packets.log", "12102026_18102026.log"])
        self.assertEqual(list_logs(self.dir)[0]["week_end"], date(2026, 10, 25))
        self.assertIsNotNone(log_path(self.dir, "19102026_25102026.log"))
        for bad in ["../etc/passwd", "log-level", "19102026_25102026.log/..", "99999999_99999999.log"]:
            self.assertIsNone(log_path(self.dir, bad), bad)


if __name__ == "__main__":
    unittest.main()
