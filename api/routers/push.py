"""
Web Push subscriptions.

Trackers never reach these -- this is the dashboard subscribing a browser to
receive a notification when a device it can see starts or stops moving. See
push.py for how a subscription actually turns into a delivered message.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ..config import ApiConfig
from ..deps import current_user, get_config, get_users
from ..schemas import PushSubscribeRequest, PushUnsubscribeRequest, VapidKeyOut
from ..users_repository import AuthenticatedUser, UserRepository

router = APIRouter(prefix="/push", tags=["push"])

_NOT_CONFIGURED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="Push is not configured; set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY on the server",
)


@router.get(
    "/vapid-public-key",
    response_model=VapidKeyOut,
    summary="The public key browsers need to create a push subscription",
    responses={503: {"description": "The server has no VAPID key pair configured."}},
)
async def vapid_public_key(config: ApiConfig = Depends(get_config)) -> VapidKeyOut:
    """
    Public by design -- a VAPID public key identifies the sender the way an
    app's package name does. It is meant to be embedded in a client, not kept
    secret; only the private key is. No sign-in required, so the frontend can
    fetch it before asking the user to turn notifications on.
    """
    if not config.vapid_public_key:
        raise _NOT_CONFIGURED
    return VapidKeyOut(public_key=config.vapid_public_key)


@router.post(
    "/subscribe",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register this browser for push notifications",
)
async def subscribe(
    payload: PushSubscribeRequest,
    user: AuthenticatedUser = Depends(current_user),
    users: UserRepository = Depends(get_users),
    config: ApiConfig = Depends(get_config),
) -> None:
    if not config.vapid_public_key:
        raise _NOT_CONFIGURED
    await users.add_subscription(user.id, payload.endpoint, payload.keys.p256dh, payload.keys.auth)


@router.post(
    "/unsubscribe",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop sending this browser push notifications",
)
async def unsubscribe(
    payload: PushUnsubscribeRequest,
    # Any signed-in account may remove a subscription by endpoint, not only
    # the one that created it -- the endpoint URL itself is unguessable, so
    # this trades a purely theoretical scoping gap for not having to handle
    # "your session expired right as you tried to turn this off".
    user: AuthenticatedUser = Depends(current_user),
    users: UserRepository = Depends(get_users),
) -> None:
    await users.remove_subscription(payload.endpoint)
