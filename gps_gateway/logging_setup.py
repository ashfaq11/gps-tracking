"""Gateway logging: console, plus the weekly file it shares with the API.

In `log_dir` (outside the project; see weekly_logs.py):
- 19102026_25102026.log          the week's warnings and errors, the API's too
- 19102026_25102026.packets.log  one human-readable line per packet received
                                 from a tracker, and per command sent to one
                                 (message_log.py) -- only with
                                 GATEWAY_MESSAGE_LOG=on

One file per week, Monday to Sunday (IST), the last `log_retention_weeks`
kept. The level -- ERROR by default -- can be changed through the API's
PUT /logs/level without a restart: `watch_level` re-reads it.
"""

import asyncio
import logging
import os

from weekly_logs import LEVEL_POLL_SECONDS, IstFormatter, LevelWatcher, WeeklyFileHandler

from .config import Config

FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
MESSAGE_LOGGER = "gps_gateway.messages"

log = logging.getLogger(__name__)


def configure_logging(config: Config) -> LevelWatcher:
    """Log to stderr and, unless `log_dir` is empty, to the week's file(s).

    The per-packet message log goes to its own file only -- a fleet's
    heartbeats would drown the console -- except when there is no log
    directory, when it falls back to the console so it is still visible.
    """
    logging.basicConfig(level=config.log_level, format=FORMAT)
    level = LevelWatcher(config.log_dir, config.log_level)
    # basicConfig leaves an already-configured root alone, level included.
    level.apply()
    messages = logging.getLogger(MESSAGE_LOGGER)
    messages.disabled = not config.message_log
    # Asked for explicitly, so written whatever the general level is.
    messages.setLevel(logging.INFO if config.message_log else logging.NOTSET)
    if not config.log_dir:
        return level

    try:
        main = WeeklyFileHandler(config.log_dir, config.log_retention_weeks)
        packets = (
            WeeklyFileHandler(config.log_dir, config.log_retention_weeks, kind="packets")
            if config.message_log
            else None
        )
    except OSError as exc:
        # An unwritable log directory must not stop the gateway taking fixes.
        log.warning("File logging disabled: cannot write to %r (%s)", config.log_dir, exc)
        return level

    main.setFormatter(IstFormatter(FORMAT))
    logging.getLogger().addHandler(main)
    if packets is not None:
        packets.setFormatter(IstFormatter("%(asctime)s %(message)s"))
        messages.addHandler(packets)
        messages.propagate = False
    log.info(
        "Logging warnings and errors to %s, one file per week, kept %d weeks%s",
        os.path.abspath(config.log_dir),
        config.log_retention_weeks,
        " (packet log on)" if packets is not None else "",
    )
    return level


async def watch_level(level: LevelWatcher) -> None:
    """Follows a level changed through the API, without a restart."""
    while True:
        await asyncio.sleep(LEVEL_POLL_SECONDS)
        if level.apply():
            # WARNING so the change itself is on record whenever it lets warnings in.
            log.warning("Log level is now %s", level.current)
