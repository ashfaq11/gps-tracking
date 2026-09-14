"""Sink interface: swap destinations without touching parser code."""

import asyncio

from ..models import LocationEvent


class Sink:
    async def start(self) -> None:
        pass

    async def publish(self, event: LocationEvent) -> None:
        raise NotImplementedError

    async def publish_many(self, events: list[LocationEvent]) -> list[BaseException | None]:
        """
        Write several events, reporting each one's outcome in order: None if
        it was written, the exception if not. Does not raise for a single bad
        event, so one rejected row cannot take the rest of its batch with it.

        This default publishes one at a time; a sink that can write a batch in
        a single round trip (PostgresSink) overrides it.
        """
        results: list[BaseException | None] = []
        for event in events:
            try:
                await self.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                results.append(exc)
            else:
                results.append(None)
        return results

    async def stop(self) -> None:
        pass
