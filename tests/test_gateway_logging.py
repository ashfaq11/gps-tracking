import logging
import os
import tempfile
import unittest
from datetime import datetime

from gps_gateway.config import Config
from gps_gateway.logging_setup import MESSAGE_LOGGER, configure_logging
from weekly_logs import IST, WeeklyFileHandler, week_file, week_start, write_level


class TestGatewayLogging(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self._saved = (self.root.handlers[:], self.root.level)
        self.root.handlers = []
        # configure_logging also gives the per-packet logger its own file.
        self.messages = logging.getLogger(MESSAGE_LOGGER)
        self._saved_messages = (self.messages.handlers[:], self.messages.propagate)
        self.messages.handlers = []

    def tearDown(self):
        for handler in self.root.handlers + self.messages.handlers:
            handler.close()
        self.root.handlers, self.root.level = self._saved
        self.messages.handlers, self.messages.propagate = self._saved_messages
        self.messages.disabled = False
        self.messages.setLevel(logging.NOTSET)

    def _file_handlers(self):
        return [h for h in self.root.handlers if isinstance(h, WeeklyFileHandler)]

    def test_defaults_keep_four_weeks_outside_the_project(self):
        config = Config.from_env({})
        self.assertEqual(config.log_dir, os.path.expanduser("~/gps_gateway/logs"))
        self.assertEqual(config.log_retention_weeks, 4)

    def test_retention_is_never_below_one_week(self):
        self.assertEqual(Config.from_env({"GATEWAY_LOG_RETENTION_WEEKS": "0"}).log_retention_weeks, 1)

    def test_writes_warnings_and_errors_to_the_weeks_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "nested", "logs")
            configure_logging(Config(log_dir=log_dir, log_level="INFO"))
            (handler,) = self._file_handlers()
            logging.getLogger("gps_gateway.test").info("device 123 logged in")
            logging.getLogger("gps_gateway.test").warning("device 123 sent junk")
            name = week_file(week_start(datetime.now(IST)))
            with open(os.path.join(log_dir, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("[gps_gateway.test] device 123 sent junk", text)
            self.assertNotIn("logged in", text)

    def test_follows_a_level_saved_through_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            level = configure_logging(Config(log_dir=tmp))
            self.assertEqual(self.root.level, logging.ERROR)
            write_level(tmp, "WARNING")
            self.assertTrue(level.apply())
            self.assertEqual(self.root.level, logging.WARNING)

    def test_only_errors_and_no_packet_log_by_default(self):
        config = Config.from_env({})
        self.assertEqual(config.log_level, "ERROR")
        self.assertFalse(config.message_log)
        configure_logging(Config(log_dir=""))
        self.assertFalse(logging.getLogger("gps_gateway.session").isEnabledFor(logging.WARNING))
        self.assertTrue(logging.getLogger("gps_gateway.session").isEnabledFor(logging.ERROR))
        self.assertTrue(self.messages.disabled)

    def test_packet_log_still_writes_when_turned_on(self):
        configure_logging(Config(log_dir="", message_log=True))
        self.assertFalse(self.messages.disabled)
        self.assertTrue(self.messages.isEnabledFor(logging.INFO))

    def test_empty_log_dir_disables_file_logging(self):
        configure_logging(Config(log_dir=""))
        self.assertEqual(self._file_handlers(), [])

    def test_unwritable_log_dir_falls_back_to_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "not-a-dir")
            open(blocker, "w").close()
            with self.assertLogs("gps_gateway.logging_setup", "WARNING"):
                configure_logging(Config(log_dir=os.path.join(blocker, "logs")))
        self.assertEqual(self._file_handlers(), [])


if __name__ == "__main__":
    unittest.main()
