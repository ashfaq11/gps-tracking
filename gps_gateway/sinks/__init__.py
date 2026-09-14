"""Downstream destinations for decoded events."""

from ..config import Config
from .base import Sink
from .batching import BatchingSink
from .log import LogSink
from .postgres import PostgresSink

__all__ = ["Sink", "BatchingSink", "LogSink", "PostgresSink", "build_sink"]


def build_sink(config: Config) -> Sink:
    if config.sink == "log":
        return LogSink()
    if config.sink == "postgres":
        sink = PostgresSink(
            config.pg_dsn,
            min_size=config.pg_pool_min,
            max_size=config.pg_pool_max,
            statement_cache_size=config.pg_statement_cache_size,
        )
        # A batch size of 1 means batching off: one INSERT per packet, with the
        # ACK waiting for it, as before.
        if config.batch_size <= 1:
            return sink
        return BatchingSink(
            sink,
            batch_size=config.batch_size,
            flush_interval_s=config.flush_interval_s,
            queue_max=config.queue_max,
        )
    raise ValueError(f"Unknown GATEWAY_SINK {config.sink!r}; expected 'postgres' or 'log'")
