"""
Web Push: VAPID key generation, sending one message, and the background
listener that turns a `vehicle_motion` database notification into a push.

    python -m api.push genkey

prints a fresh VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY pair for the environment.

The listener needs a process that keeps running -- it holds one dedicated
Postgres connection open for `LISTEN` and reacts to notifications as they
arrive. That works when `python -m api` (or any host that runs ASGI lifespan)
is the deployment target, but **not on Vercel**: a serverless invocation is
short-lived and may never see the connection stay open long enough to receive
anything. Subscribing and unsubscribing still work everywhere, since those are
ordinary per-request database writes -- only delivery needs a long-running
process. If the REST API stays on Vercel, this listener can run as its own
small process instead, the same way the TCP gateway already does.
"""

import asyncio
import base64
import json
import logging
import sys

from .config import ApiConfig
from .pglisten import listen_forever
from .users_repository import UserRepository

log = logging.getLogger(__name__)

_CHANNEL = "vehicle_motion"


class SubscriptionGone(Exception):
    """The push service says this endpoint no longer exists."""


def generate_vapid_keypair() -> tuple[str, str]:
    """
    Return (public_key, private_key), both base64url-encoded with no padding
    -- the public key as the uncompressed EC point (0x04 || X || Y) a
    browser's `PushManager.subscribe({applicationServerKey})` expects, the
    private key as the raw 32-byte scalar `pywebpush`'s `Vapid.from_string`
    accepts directly. Neither needs a PEM file on disk.
    """
    from py_vapid import Vapid02

    vapid = Vapid02()
    vapid.generate_keys()

    private_value = vapid.private_key.private_numbers().private_value
    private_bytes = private_value.to_bytes(32, "big")

    numbers = vapid.public_key.public_numbers()
    public_bytes = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")

    return _b64url(public_bytes), _b64url(private_bytes)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def send_push(endpoint: str, p256dh: str, auth: str, payload: dict, config: ApiConfig) -> None:
    """
    Send one push message.

    Synchronous and blocking -- `pywebpush` sends the request with `requests`
    -- so callers must run this off the event loop (`asyncio.to_thread` below,
    or a sync FastAPI `BackgroundTasks` callback, which Starlette already
    threadpools).
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=json.dumps(payload),
            vapid_private_key=config.vapid_private_key,
            vapid_claims={"sub": config.vapid_subject},
        )
    except WebPushException as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (404, 410):
            # The browser uninstalled the subscription, cleared site data, or
            # it simply expired -- the push service will never accept it
            # again, so it is dead weight in the table.
            raise SubscriptionGone from exc
        log.warning("Push failed (status=%s): %s", status_code, exc)


async def run_motion_listener(config: ApiConfig, users: UserRepository) -> None:
    """
    Forward `vehicle_motion` notifications (see the trigger in sql/schema.sql)
    to every push subscription that can see the device. Runs for the life of
    the process; cancel the returned task to stop it. See `pglisten.py` for
    the reconnect/keepalive shape this relies on.
    """
    await listen_forever(config, _CHANNEL, lambda event: _dispatch(event, users, config))


async def _dispatch(event: dict, users: UserRepository, config: ApiConfig) -> None:
    device_id = event.get("device_id")
    if not device_id:
        log.warning("%s event had no device_id: %r", _CHANNEL, event)
        return

    log.info("%s: device=%s moving=%s speed=%s", _CHANNEL, device_id, event.get("moving"), event.get("speed_kmh"))

    subscriptions = await users.subscriptions_for_device(device_id)
    if not subscriptions:
        log.info("No push subscriptions can see device=%s; nothing to send", device_id)
        return

    moving = bool(event.get("moving"))
    speed = event.get("speed_kmh")
    payload = {
        "title": f"{device_id} started moving" if moving else f"{device_id} stopped",
        "body": f"Now travelling at {speed} km/h" if moving else "Speed has dropped to 0 km/h",
        "device_id": device_id,
    }

    for sub in subscriptions:
        try:
            await asyncio.to_thread(send_push, sub.endpoint, sub.p256dh, sub.auth, payload, config)
            log.info("Push sent for device=%s to endpoint=...%s", device_id, sub.endpoint[-12:])
        except SubscriptionGone:
            log.info("Subscription gone (endpoint=...%s); removing it", sub.endpoint[-12:])
            await users.remove_subscription(sub.endpoint)
        except Exception:
            log.exception("Unexpected error sending push to a subscription")


def _main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "genkey":
        print("usage: python -m api.push genkey", file=sys.stderr)
        raise SystemExit(2)
    public_key, private_key = generate_vapid_keypair()
    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY={private_key}")


if __name__ == "__main__":
    _main()
