"""Gateway logging: console, plus a daily-rotated file kept for a fixed window."""

import logging
import logging.handlers
import os

from .config import Config

FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_FILE_NAME = "gateway.log"


def configure_logging(config: Config) -> None:
    """Log to stderr and, unless disabled, to `<log_dir>/gateway.log`.

    The file rolls over at local midnight (`gateway.log.2026-09-26`) and
    anything older than `log_retention_days` is deleted at rollover, so the
    directory is bounded without a cron job or logrotate.
    """
    logging.basicConfig(level=logging.INFO, format=FORMAT)
    if not config.log_dir:
        return

    try:
        os.makedirs(config.log_dir, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            os.path.join(config.log_dir, LOG_FILE_NAME),
            when="midnight",
            backupCount=config.log_retention_days,
            encoding="utf-8",
        )
    except OSError as exc:
        # An unwritable log directory must not stop the gateway taking fixes.
        logging.getLogger(__name__).warning(
            "File logging disabled: cannot write to %r (%s)", config.log_dir, exc
        )
        return

    handler.setFormatter(logging.Formatter(FORMAT))
    logging.getLogger().addHandler(handler)
