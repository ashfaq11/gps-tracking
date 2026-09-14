"""Console sink -- zero dependencies, for local testing."""

import logging
from dataclasses import asdict

from ..models import LocationEvent
from .base import Sink

log = logging.getLogger(__name__)


class LogSink(Sink):
    def __init__(self):
        self.published = 0

    async def publish(self, event: LocationEvent) -> None:
        self.published += 1
        log.info("EVENT %s", asdict(event))
