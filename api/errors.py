"""
Error handling.

Every failure is logged with its request id and answered with a JSON body
carrying that same id, so a user reporting "it broke" gives you the exact
line to grep for.
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .middleware import request_id_var

log = logging.getLogger("api.error")


def _request_id(request: Request) -> str:
    """
    Prefer the id recorded on the scope; the contextvar is already reset by
    the time an unhandled exception reaches its handler.
    """
    return getattr(request.state, "request_id", None) or request_id_var.get()


def _body(detail, request_id: str) -> dict:
    return {"detail": detail, "request_id": request_id}


def _headers(request_id: str, extra: dict | None = None) -> dict:
    """Error responses from the outermost handler bypass the middleware that
    normally stamps this header, so set it here too."""
    return {**(extra or {}), "X-Request-ID": request_id}


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Deliberate 4xx/5xx raised by our own code."""
    request_id = _request_id(request)
    # 5xx we raised ourselves is still a server problem worth an ERROR line.
    level = logging.ERROR if exc.status_code >= 500 else logging.WARNING
    log.log(
        level,
        "%s %s -> %d: %s",
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
        extra={"request_id": request_id},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(exc.detail, request_id),
        headers=_headers(request_id, getattr(exc, "headers", None)),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    A 422 means the caller sent something we could not parse.

    Log which fields failed -- without that, a client integration failing
    against this endpoint is guesswork from both sides.
    """
    request_id = _request_id(request)
    problems = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ())),
            "error": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    log.warning(
        "%s %s -> 422: %s",
        request.method,
        request.url.path,
        "; ".join(f"{p['field']}: {p['error']}" for p in problems) or "invalid payload",
        extra={"request_id": request_id},
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_body(problems, request_id),
        headers=_headers(request_id),
    )


async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Anything we did not anticipate: a dropped database connection, a bug.

    The traceback goes to the log; the client gets an opaque message plus the
    request id. Internal details must not leak into the response body.
    """
    request_id = _request_id(request)
    log.exception(
        "%s %s -> 500 unhandled %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        extra={"request_id": request_id},
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_body("Internal server error", request_id),
        headers=_headers(request_id),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
