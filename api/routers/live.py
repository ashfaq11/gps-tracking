"""
Live location over WebSocket.

Mounted under the same `/api/v1` prefix as everything else, even though a
WebSocket upgrade is a different kind of request: it means a reverse proxy
or dev-server rule that already forwards `/api/*` needs no second rule for
this one path.

Outside the normal `Authorization` header model, though -- a browser's
WebSocket API cannot set one, so the token travels as a query parameter
instead (`?token=...`). That is weaker than a header in one specific way (it
can end up in a server access log), which is why nothing else in this API
accepts a token this way; the trade is unavoidable for a plain browser
WebSocket.
"""

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..state import ensure_users
from ..users_repository import AuthenticatedUser

log = logging.getLogger(__name__)

router = APIRouter(tags=["live"])

# Close codes in the 4000-4999 range are reserved for applications.
_UNAUTHENTICATED = 4401


def _scope(user: AuthenticatedUser) -> frozenset[str] | None:
    return None if user.is_admin else frozenset(user.devices)


@router.websocket("/ws/live")
async def live_location(websocket: WebSocket, token: str = Query(...)) -> None:
    """
    One fix, pushed the instant it lands, for every device this connection's
    account can see. Nothing meaningful is ever expected *from* the client --
    this socket only reads frames so it notices when the browser closes it.
    """
    users = await ensure_users(websocket.app)
    user = await users.resolve_token(token)
    if user is None:
        # Rejecting before accept() denies the handshake outright, rather
        # than accepting a connection only to immediately drop it.
        await websocket.close(code=_UNAUTHENTICATED)
        return

    await websocket.accept()
    manager = websocket.app.state.live_connections
    await manager.register(websocket, _scope(user))
    log.info(
        "Live location connected: user=%s scope=%s (now %d)",
        user.username,
        "all" if user.is_admin else len(user.devices),
        manager.count,
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.unregister(websocket)
        log.info("Live location disconnected: user=%s (now %d)", user.username, manager.count)
