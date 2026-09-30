"""`python -m api` writes api.log, rotated daily and kept at most 30 days."""

import logging
import os
import tempfile
import unittest

try:
    from api.__main__ import LOG_FILE_NAME, _configure_logging
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

    def test_writes_ist_stamped_lines_with_the_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            _configure_logging(ApiConfig(log_dir=tmp))
            logging.getLogger("api.test").warning("hello from a test")
            for handler in self.root.handlers:
                handler.flush()
            with open(os.path.join(tmp, LOG_FILE_NAME), encoding="utf-8") as f:
                text = f.read()
            self.assertRegex(
                text, r"\d\d:\d\d:\d\d IST WARNING \[api\.test\] \[-\] hello from a test"
            )
            rotating = [h for h in self.root.handlers if hasattr(h, "backupCount")]
            self.assertEqual(rotating[0].backupCount, 30)

    def test_retention_is_capped_at_30_days(self):
        self.assertEqual(
            ApiConfig.from_env({"API_LOG_RETENTION_DAYS": "90"}).log_retention_days, 30
        )
        self.assertEqual(ApiConfig.from_env({}).log_retention_days, 30)


if __name__ == "__main__":
    unittest.main()
