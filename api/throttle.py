"""
Sign-in throttling: slows password guessing without locking anyone out.

Failures are counted in a sliding window per (username, client address) and
per username alone. The pair limit stops one source hammering an account;
the looser per-username limit still holds when the address is spoofed or
rotated, while being high enough that a stranger guessing cannot lock the
real owner out for long -- the window slides, nothing is permanent.

Kept in memory, per process: the API runs as one long-lived process on the
server, and a restart forgetting the counts is acceptable. (On serverless,
where each instance has its own memory, it is best-effort only.)
"""

import time
from collections import deque

from fastapi import Request


def client_address(request: Request) -> str:
    """The caller's address, behind the Cloudflare worker if there is one."""
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class LoginThrottle:
    def __init__(
        self,
        per_source: int = 10,
        per_username: int = 50,
        window_seconds: float = 900.0,
        clock=time.monotonic,
    ) -> None:
        self.per_source = per_source
        self.per_username = per_username
        self.window = window_seconds
        self.clock = clock
        self._failures: dict[tuple[str, ...], deque[float]] = {}

    def retry_after(self, username: str, address: str) -> int:
        """Seconds until a sign-in may be tried again; 0 when it may now."""
        user = username.strip().lower()
        waits = [
            self._wait((user, address), self.per_source),
            self._wait((user,), self.per_username),
        ]
        return max(waits)

    def failed(self, username: str, address: str) -> None:
        user = username.strip().lower()
        now = self.clock()
        for key in ((user, address), (user,)):
            self._failures.setdefault(key, deque()).append(now)
        if len(self._failures) > 10_000:
            self._sweep(now)

    def succeeded(self, username: str, address: str) -> None:
        self._failures.pop((username.strip().lower(), address), None)

    def _wait(self, key: tuple[str, ...], limit: int) -> int:
        times = self._failures.get(key)
        if not times:
            return 0
        now = self.clock()
        while times and now - times[0] >= self.window:
            times.popleft()
        if len(times) < limit:
            return 0
        return max(1, int(self.window - (now - times[0])) + 1)

    def _sweep(self, now: float) -> None:
        for key in [k for k, t in self._failures.items() if not t or now - t[-1] >= self.window]:
            del self._failures[key]
