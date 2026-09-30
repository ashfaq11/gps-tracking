"""Gateway logging: console, plus daily-rotated files kept for a fixed window.

Two files in `log_dir`:
- gateway.log           everything the gateway logs (connections, errors, ...)
- gateway-messages.log  one human-readable line per packet received from a
                        tracker, and per command sent to one (message_log.py)

Both roll over at midnight (`gateway.log.2026-09-26`) and anything older than
`log_retention_days` (at most 30) is deleted at rollover, so the directory is
bounded without a cron job or logrotate. File timestamps are IST, like the
dashboard.
"""

import logging
import logging.handlers
import os
from datetime import datetime

from .config import Config
from .message_log import IST

FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_FILE_NAME = "gateway.log"
MESSAGE_LOG_FILE_NAME = "gateway-messages.log"
MESSAGE_LOGGER = "gps_gateway.messages"


class IstFormatter(logging.Formatter):
    """Timestamps as `2026-09-30 11:25:32 IST`, whatever the server's timezone."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging's API
        return datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _rotating(log_dir: str, name: str, days: int, fmt: str) -> logging.Handler:
    handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, name),
        when="midnight",
        backupCount=days,
        encoding="utf-8",
    )
    handler.setFormatter(IstFormatter(fmt))
    return handler


def configure_logging(config: Config) -> None:
    """Log to stderr and, unless `log_dir` is empty, to the two files above.

    The per-packet message log goes to its own file only -- a fleet's
    heartbeats would drown the console -- except when there is no log
    directory, when it falls back to the console so it is still visible.
    """
    logging.basicConfig(level=logging.INFO, format=FORMAT)
    messages = logging.getLogger(MESSAGE_LOGGER)
    messages.disabled = not config.message_log
    if not config.log_dir:
        return

    try:
        os.makedirs(config.log_dir, exist_ok=True)
        main = _rotating(config.log_dir, LOG_FILE_NAME, config.log_retention_days, FORMAT)
        packets = _rotating(
            config.log_dir,
            MESSAGE_LOG_FILE_NAME,
            config.log_retention_days,
            "%(asctime)s %(message)s",
        )
    except OSError as exc:
        # An unwritable log directory must not stop the gateway taking fixes.
        logging.getLogger(__name__).warning(
            "File logging disabled: cannot write to %r (%s)", config.log_dir, exc
        )
        return

    logging.getLogger().addHandler(main)
    messages.addHandler(packets)
    messages.propagate = False
    logging.getLogger(__name__).info(
        "Logging to %s (%s, %s), kept %d days",
        os.path.abspath(config.log_dir),
        LOG_FILE_NAME,
        MESSAGE_LOG_FILE_NAME if config.message_log else "message log off",
        config.log_retention_days,
    )
