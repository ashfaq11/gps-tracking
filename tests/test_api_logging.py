"""`python -m api` writes warnings and errors to the week's shared file, outside the project."""

import logging
import os
import tempfile
import unittest
from datetime import datetime

from weekly_logs import IST, week_file, week_start, write_level

try:
    from api.__main__ import _configure_logging
    from api.config import ApiConfig

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed")
class TestApiLogFile(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.saved = (list(self.root.handlers), self.root.level)

    def tearDown(self):
        for handler in self.root.handlers:
            if handler not in self.saved[0]:
                handler.close()
        self.root.handlers = self.saved[0]
        self.root.setLevel(self.saved[1])
        logging.getLogger("uvicorn.access").disabled = False

    def week_text(self, folder: str) -> str:
        with open(os.path.join(folder, week_file(week_start(datetime.now(IST)))), encoding="utf-8") as f:
            return f.read()

    def test_errors_go_to_the_weeks_file_with_the_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_logging(ApiConfig(log_dir=tmp))
            logging.getLogger("api.test").warning("not at ERROR")
            logging.getLogger("api.test").error("hello from a test")
            text = self.week_text(tmp)
            self.assertRegex(text, r"\d\d:\d\d:\d\d IST ERROR   \[api\.test\] \[-\] hello from a test")
            self.assertNotIn("not at ERROR", text)

    def test_a_level_saved_through_the_api_wins_over_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_level(tmp, "WARNING")
            _configure_logging(ApiConfig(log_dir=tmp, log_level="ERROR"))
            logging.getLogger("api.test").warning("careful")
            logging.getLogger("api.test").info("chatty")
            text = self.week_text(tmp)
            self.assertIn("careful", text)
            self.assertNotIn("chatty", text)

    def test_defaults_errors_only_kept_4_weeks_outside_the_project(self):
        config = ApiConfig.from_env({})
        self.assertEqual(config.log_level, "ERROR")
        self.assertEqual(config.log_retention_weeks, 4)
        self.assertEqual(config.log_dir, os.path.expanduser("~/gps_gateway/logs"))
        self.assertEqual(ApiConfig.from_env({"API_LOG_RETENTION_WEEKS": "40"}).log_retention_weeks, 12)

    def test_only_errors_by_default(self):
        _configure_logging(ApiConfig(log_dir=""))
        self.assertFalse(logging.getLogger("api.access").isEnabledFor(logging.WARNING))
        self.assertTrue(logging.getLogger("api.access").isEnabledFor(logging.ERROR))

    def test_level_can_be_raised_and_ignores_nonsense(self):
        self.assertEqual(ApiConfig.from_env({"API_LOG_LEVEL": "info"}).log_level, "INFO")
        self.assertEqual(ApiConfig.from_env({"API_LOG_LEVEL": "loud"}).log_level, "ERROR")


if __name__ == "__main__":
    unittest.main()
