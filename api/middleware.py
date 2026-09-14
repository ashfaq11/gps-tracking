"""Request logging."""

import logging
import time
from contextvars import ContextVar
from uuid import uuid4

from starlette.datastructures import MutableHeaders

log = logging.getLogger("api.access")

# Set per request so any log line emitted while handling it can be traced
# back, not just the access line the middleware writes itself.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Health checks fire constantly from load balancers; logging them at INFO
# buries real traffic.
_QUIET_PATHS = {"/api/v1/health"}


class RequestIdFilter(logging.Filter):
    """
    Makes %(request_id)s available to every formatter in the process, so log
    lines from application code are correlated too, not just access lines.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class RequestLoggingMiddleware:
    """
    Logs one line per request, after the response status is known.

    Written as raw ASGI rather than BaseHTTPMiddleware so it does not buffer
    response bodies or spawn a task per request.
    """

    def __init__(self, app, slow_request_ms: float = 1000.0):
        self.app = app
        self.slow_request_ms = slow_request_ms

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex[:12]
        # Also stored on the scope because Starlette's ServerErrorMiddleware
        # sits outside this one: on an unhandled exception it runs the 500
        # handler after this middleware's finally has reset the contextvar,
        # so the handler needs somewhere else to read the id from.
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            log.exception(
                "%s failed after %.1fms",
                self._describe(scope),
                elapsed,
                extra={"request_id": request_id},
            )
            raise
        else:
            elapsed = (time.perf_counter() - started) * 1000
            log.log(
                self._level(scope, status_code, elapsed),
                "%s -> %d in %.1fms",
                self._describe(scope),
                status_code,
                elapsed,
                # Set on the record directly, not only through RequestIdFilter,
                # so correlation survives a host that installs its own logging
                # config (gunicorn, a container platform) and never adds the
                # filter.
                extra={"request_id": request_id},
            )
        finally:
            request_id_var.reset(token)

    def _level(self, scope, status_code: int, elapsed_ms: float) -> int:
        if status_code >= 500:
            return logging.ERROR
        if status_code >= 400 or elapsed_ms >= self.slow_request_ms:
            return logging.WARNING
        if scope["path"] in _QUIET_PATHS:
            return logging.DEBUG
        return logging.INFO

    @staticmethod
    def _describe(scope) -> str:
        path = scope["path"]
        query = scope.get("query_string", b"").decode()
        client = scope.get("client")
        who = client[0] if client else "-"
        # The API key travels in a header, so nothing secret is in the URL.
        return f"{who} {scope['method']} {path}{'?' + query if query else ''}"
