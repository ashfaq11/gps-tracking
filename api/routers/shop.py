"""
The device shop: customers order trackers in the app, admins fulfil them.

Paid cash on delivery -- there is no payment step. The rules, once for
both backends:

- Products: everyone signed in sees those on sale; admins also see hidden
  ones and are the only ones who add or change them.
- Orders: a customer sees only their own, and gets the same 404 for "not
  yours" as for "does not exist" (as every device-scoped endpoint does).
  They may cancel while the order is still `placed`.
- Admins move an order placed -> confirmed -> shipped -> delivered, or
  cancel it; a delivered or cancelled order's status is final.
- IMEIs entered on a shipped or delivered order are claimed for the
  customer, so the trackers appear in their account. An IMEI someone else
  already owns refuses the whole change (409), before anything is claimed.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import current_user, get_shop, get_users, require_admin
from ..schemas import (
    OrderCreate,
    OrderOut,
    OrderStatus,
    OrderUpdate,
    ProductIn,
    ProductOut,
    ProductUpdate,
)
from ..shop_repository import ShopRepository
from ..users_repository import AuthenticatedUser, UserRepository

router = APIRouter(prefix="/shop", tags=["shop"])

FINAL: frozenset[str] = frozenset({"delivered", "cancelled"})
# Statuses whose trackers are on their way or arrived -- the ones whose
# IMEIs are claimed for the customer.
CLAIMING: frozenset[str] = frozenset({"shipped", "delivered"})


def _not_found(order_id: int) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No order {order_id}")


def _for(user: AuthenticatedUser, order: OrderOut) -> OrderOut:
    """The admin-only note stays with admins."""
    return order if user.is_admin else order.model_copy(update={"admin_note": None})


async def _visible_order(order_id: int, user: AuthenticatedUser, shop: ShopRepository) -> OrderOut:
    order = await shop.get_order(order_id)
    if order is None or (not user.is_admin and order.user_id != user.id):
        raise _not_found(order_id)
    return order


# --- products ----------------------------------------------------------------


@router.get("/products", response_model=list[ProductOut], summary="Trackers on sale")
async def list_products(
    user: AuthenticatedUser = Depends(current_user),
    shop: ShopRepository = Depends(get_shop),
):
    """Cheapest first. Admins also get the hidden ones (`active: false`)."""
    return await shop.list_products(include_inactive=user.is_admin)


@router.post(
    "/products",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    summary="Put a tracker on sale (admin)",
    dependencies=[Depends(require_admin)],
)
async def create_product(payload: ProductIn, shop: ShopRepository = Depends(get_shop)):
    return await shop.create_product(payload)


@router.patch(
    "/products/{product_id}",
    response_model=ProductOut,
    summary="Change or hide a product (admin)",
    dependencies=[Depends(require_admin)],
    responses={404: {"description": "No such product."}},
)
async def update_product(
    product_id: int, payload: ProductUpdate, shop: ShopRepository = Depends(get_shop)
):
    """A new price applies to new orders; existing orders keep theirs.
    Products are hidden (`active: false`) rather than deleted, since orders
    refer to them."""
    product = await shop.update_product(product_id, payload)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No product {product_id}")
    return product


# --- orders ------------------------------------------------------------------


@router.post(
    "/orders",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Order trackers (cash on delivery)",
    responses={404: {"description": "The product is not on sale."}},
)
async def create_order(
    payload: OrderCreate,
    user: AuthenticatedUser = Depends(current_user),
    shop: ShopRepository = Depends(get_shop),
) -> OrderOut:
    """At today's price, paid in cash when it arrives. Starts `placed`."""
    product = await shop.get_product(payload.product_id)
    if product is None or not product.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That product is not on sale")
    order = await shop.create_order(user.id, user.username, product, payload)
    return _for(user, order)


@router.get("/orders", response_model=list[OrderOut], summary="Orders")
async def list_orders(
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
    user: AuthenticatedUser = Depends(current_user),
    shop: ShopRepository = Depends(get_shop),
):
    """Newest first: the caller's own, or every customer's for an admin."""
    orders = await shop.list_orders(
        user_id=None if user.is_admin else user.id, status=status_filter
    )
    return [_for(user, o) for o in orders]


@router.get(
    "/orders/{order_id}",
    response_model=OrderOut,
    summary="One order, with its timeline",
    responses={404: {"description": "No such order (or not yours)."}},
)
async def get_order(
    order_id: int,
    user: AuthenticatedUser = Depends(current_user),
    shop: ShopRepository = Depends(get_shop),
):
    return _for(user, await _visible_order(order_id, user, shop))


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderOut,
    summary="Cancel an order",
    responses={
        404: {"description": "No such order (or not yours)."},
        409: {"description": "Too late to cancel: already confirmed, shipped or final."},
    },
)
async def cancel_order(
    order_id: int,
    user: AuthenticatedUser = Depends(current_user),
    shop: ShopRepository = Depends(get_shop),
) -> OrderOut:
    """A customer can cancel until the order is confirmed; an admin until it
    is delivered."""
    order = await _visible_order(order_id, user, shop)
    if order.status == "cancelled":
        return _for(user, order)
    allowed = order.status not in FINAL if user.is_admin else order.status == "placed"
    if not allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"This order is already {order.status} and can no longer be cancelled.",
        )
    updated = await shop.update_order(order_id, {"status": "cancelled"}, user.id, user.username)
    return _for(user, updated)


@router.patch(
    "/orders/{order_id}",
    response_model=OrderOut,
    summary="Update an order (admin)",
    responses={
        404: {"description": "No such order."},
        409: {
            "description": (
                "The status is final, or an IMEI is already registered to another account."
            )
        },
    },
)
async def update_order(
    order_id: int,
    payload: OrderUpdate,
    admin: AuthenticatedUser = Depends(require_admin),
    shop: ShopRepository = Depends(get_shop),
    users: UserRepository = Depends(get_users),
) -> OrderOut:
    """
    Move an order along (`placed` -> `confirmed` -> `shipped` ->
    `delivered`, or `cancelled`), record its courier `tracking`, or keep an
    `admin_note`. A delivered or cancelled order's status cannot change, but
    its other fields can.

    `device_ids` are the IMEIs of the trackers sent. On a shipped or
    delivered order they are claimed for the customer, who then sees the
    trackers in their account -- no need to type the IMEI in the app.
    """
    order = await shop.get_order(order_id)
    if order is None:
        raise _not_found(order_id)

    changes = payload.model_dump(exclude_unset=True, exclude={"note"})
    new_status = changes.get("status", order.status)
    if new_status != order.status and order.status in FINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"This order is {order.status}; its status can no longer change.",
        )
    if "device_ids" in changes:
        changes["device_ids"] = list(dict.fromkeys(changes["device_ids"]))

    device_ids = changes.get("device_ids", order.device_ids)
    if new_status in CLAIMING and device_ids:
        # Every IMEI checked before any is claimed, so a conflict leaves
        # nothing half-assigned.
        to_claim = []
        for device_id in device_ids:
            owner = await users.device_owner(device_id)
            if owner is None:
                to_claim.append(device_id)
            elif owner != order.user_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail=f"{device_id} is already registered to another account.",
                )
        for device_id in to_claim:
            if not await users.claim_device(order.user_id, device_id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail=f"{device_id} was just registered to another account.",
                )

    return await shop.update_order(order_id, changes, admin.id, admin.username, payload.note)
