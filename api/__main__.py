"""
Run the REST API: `python -m api`

Preferred over the bare `uvicorn` CLI because host and port come from the
same ApiConfig the startup banner reports, so the logged URLs are always the
addresses actually bound.
"""

import logging

import uvicorn

from .config import ApiConfig
from .main import create_app
from .middleware import RequestIdFilter


def _configure_logging() -> None:
    """Put the request id on every line, so a failure can be traced."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] [%(request_id)s] %(message)s")
    )
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Uvicorn's own access log duplicates api.access, which carries more.
    logging.getLogger("uvicorn.access").disabled = True


def main() -> None:
    _configure_logging()
    config = ApiConfig.from_env()
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
