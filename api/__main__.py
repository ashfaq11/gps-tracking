"""
Run the REST API: `python -m api`

Preferred over the bare `uvicorn` CLI because host and port come from the
same ApiConfig the startup banner reports, so the logged URLs are always the
addresses actually bound.
"""

import logging
import logging.handlers
import os
from datetime import datetime, timedelta, timezone

import uvicorn

from .config import ApiConfig
from .main import create_app
from .middleware import RequestIdFilter

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] [%(request_id)s] %(message)s"
LOG_FILE_NAME = "api.log"
# India has no daylight saving, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


class IstFormatter(logging.Formatter):
    """File timestamps as `2026-09-30 11:25:32 IST`, like the dashboard."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging's API
        return datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _configure_logging(config: ApiConfig) -> None:
    """Put the request id on every line, so a failure can be traced -- on the
    console, and in `<log_dir>/api.log` (rotated at midnight, kept
    `log_retention_days`, at most 30)."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Uvicorn's own access log duplicates api.access, which carries more.
    logging.getLogger("uvicorn.access").disabled = True

    if not config.log_dir:
        return
    try:
        os.makedirs(config.log_dir, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            os.path.join(config.log_dir, LOG_FILE_NAME),
            when="midnight",
            backupCount=config.log_retention_days,
            encoding="utf-8",
        )
    except OSError as exc:
        # An unwritable log directory must not stop the API serving.
        logging.getLogger(__name__).warning(
            "File logging disabled: cannot write to %r (%s)", config.log_dir, exc
        )
        return
    file_handler.setFormatter(IstFormatter(FORMAT))
    file_handler.addFilter(RequestIdFilter())
    root.addHandler(file_handler)
    logging.getLogger(__name__).info(
        "Logging to %s, kept %d days",
        os.path.join(os.path.abspath(config.log_dir), LOG_FILE_NAME),
        config.log_retention_days,
    )


def main() -> None:
    config = ApiConfig.from_env()
    _configure_logging(config)
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
