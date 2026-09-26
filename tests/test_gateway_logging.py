import logging
import logging.handlers
import os
import tempfile
import unittest

from gps_gateway.config import Config
from gps_gateway.logging_setup import LOG_FILE_NAME, configure_logging


class TestGatewayLogging(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self._saved = (self.root.handlers[:], self.root.level)
        self.root.handlers = []

    def tearDown(self):
        for handler in self.root.handlers:
            handler.close()
        self.root.handlers, self.root.level = self._saved

    def _file_handlers(self):
        return [
            h for h in self.root.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]

    def test_defaults_keep_two_weeks(self):
        config = Config.from_env({})
        self.assertEqual(config.log_dir, "logs")
        self.assertEqual(config.log_retention_days, 14)

    def test_retention_is_never_below_one_day(self):
        self.assertEqual(Config.from_env({"GATEWAY_LOG_RETENTION_DAYS": "0"}).log_retention_days, 1)

    def test_writes_records_to_a_daily_rotating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "nested", "logs")
            configure_logging(Config(log_dir=log_dir, log_retention_days=14))

            (handler,) = self._file_handlers()
            self.assertEqual(handler.backupCount, 14)
            self.assertEqual(handler.when, "MIDNIGHT")

            logging.getLogger("gps_gateway.test").info("device 123 logged in")
            handler.flush()
            with open(os.path.join(log_dir, LOG_FILE_NAME), encoding="utf-8") as f:
                self.assertIn("[gps_gateway.test] device 123 logged in", f.read())
            handler.close()

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
