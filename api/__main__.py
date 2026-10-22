"""
Run the REST API: `python -m api`

Preferred over the bare `uvicorn` CLI because host and port come from the
same ApiConfig the startup banner reports, so the logged URLs are always the
addresses actually bound.
"""

import logging
import os

import uvicorn

from weekly_logs import IstFormatter, WeeklyFileHandler, read_level

from .config import ApiConfig
from .main import create_app
from .middleware import RequestIdFilter

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] [%(request_id)s] %(message)s"


def _configure_logging(config: ApiConfig) -> None:
    """Put the request id on every line, so a failure can be traced -- on the
    console, and in the week's file in `log_dir` (warnings and errors only,
    shared with the gateway; see weekly_logs.py)."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    # The level saved through PUT /logs/level wins over API_LOG_LEVEL.
    root.setLevel(read_level(config.log_dir, config.log_level))
    # Uvicorn's own access log duplicates api.access, which carries more.
    logging.getLogger("uvicorn.access").disabled = True

    if not config.log_dir:
        return
    try:
        file_handler = WeeklyFileHandler(config.log_dir, config.log_retention_weeks)
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
        "Logging warnings and errors to %s, one file per week, kept %d weeks",
        os.path.abspath(config.log_dir),
        config.log_retention_weeks,
    )


def main() -> None:
    config = ApiConfig.from_env()
    _configure_logging(config)
    # log_config=None: uvicorn's loggers follow the root level, so a level set
    # through PUT /logs/level reaches them too.
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_config=None)


if __name__ == "__main__":
    main()
