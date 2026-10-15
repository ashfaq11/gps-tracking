import logging
import logging.handlers
import os
import tempfile
import unittest

from gps_gateway.config import Config
from gps_gateway.logging_setup import LOG_FILE_NAME, MESSAGE_LOGGER, configure_logging


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
        return [
            h
            for h in self.root.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]

    def test_defaults_keep_thirty_days(self):
        config = Config.from_env({})
        self.assertEqual(config.log_dir, "logs")
        self.assertEqual(config.log_retention_days, 30)

    def test_retention_is_never_below_one_day(self):
        self.assertEqual(Config.from_env({"GATEWAY_LOG_RETENTION_DAYS": "0"}).log_retention_days, 1)

    def test_writes_records_to_a_daily_rotating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "nested", "logs")
            configure_logging(Config(log_dir=log_dir, log_retention_days=14, log_level="INFO"))

            (handler,) = self._file_handlers()
            self.assertEqual(handler.backupCount, 14)
            self.assertEqual(handler.when, "MIDNIGHT")

            logging.getLogger("gps_gateway.test").info("device 123 logged in")
            handler.flush()
            with open(os.path.join(log_dir, LOG_FILE_NAME), encoding="utf-8") as f:
                self.assertIn("[gps_gateway.test] device 123 logged in", f.read())
            handler.close()

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
