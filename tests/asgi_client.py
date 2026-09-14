"""
Minimal ASGI test client.

FastAPI's own TestClient needs httpx, which is not a dependency of this
project. This drives the ASGI app directly instead, so API tests run with
nothing installed beyond fastapi itself.
"""

import json as jsonlib
from dataclasses import dataclass


@dataclass
class Response:
    status_code: int
    headers: dict
    body: bytes

    def json(self):
        return jsonlib.loads(self.body)


class AsgiClient:
    """Async context manager that runs the app's lifespan around requests."""

    def __init__(self, app):
        self.app = app
        self._lifespan_receive = None
        self._lifespan_task = None

    async def __aenter__(self):
        import asyncio

        self._startup = asyncio.Event()
        self._shutdown_requested = asyncio.Event()
        self._messages = asyncio.Queue()
        await self._messages.put({"type": "lifespan.startup"})

        async def receive():
            return await self._messages.get()

        async def send(message):
            if message["type"] in ("lifespan.startup.complete", "lifespan.startup.failed"):
                self._startup.set()
                if message["type"] == "lifespan.startup.failed":
                    raise RuntimeError(f"Lifespan startup failed: {message}")

        self._lifespan_task = asyncio.create_task(
            self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
        )
        await asyncio.wait_for(self._startup.wait(), timeout=5)
        return self

    async def __aexit__(self, *exc):
        await self._messages.put({"type": "lifespan.shutdown"})
        try:
            import asyncio

            await asyncio.wait_for(self._lifespan_task, timeout=5)
        except Exception:
            self._lifespan_task.cancel()
        return False

    async def request(self, method: str, path: str, json=None, headers=None) -> Response:
        raw_path, _, query = path.partition("?")
        body = b"" if json is None else jsonlib.dumps(json).encode()
        header_list = [(b"host", b"testserver")]
        if json is not None:
            header_list.append((b"content-type", b"application/json"))
        for key, value in (headers or {}).items():
            header_list.append((key.lower().encode(), value.encode()))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": header_list,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        sent = []
        request_delivered = False

        async def receive():
            nonlocal request_delivered
            if request_delivered:
                return {"type": "http.disconnect"}
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        # Starlette re-raises after its 500 handler has produced a response.
        # Mirror TestClient(raise_server_exceptions=False): keep the response
        # if one was sent, and only propagate when nothing came back.
        try:
            await self.app(scope, receive, send)
        except Exception:
            if not sent:
                raise

        status = 500
        headers_out = {}
        chunks = b""
        for message in sent:
            if message["type"] == "http.response.start":
                status = message["status"]
                headers_out = {k.decode(): v.decode() for k, v in message.get("headers", [])}
            elif message["type"] == "http.response.body":
                chunks += message.get("body", b"")
        return Response(status_code=status, headers=headers_out, body=chunks)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def patch(self, path, **kwargs):
        return await self.request("PATCH", path, **kwargs)

    async def put(self, path, **kwargs):
        return await self.request("PUT", path, **kwargs)

    async def delete(self, path, **kwargs):
        return await self.request("DELETE", path, **kwargs)
