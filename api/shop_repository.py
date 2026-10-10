"""
The device shop: trackers on sale (device_products) and customers' orders
for them (device_orders, with every status change in device_order_events).

Storage only. Who may do what -- a customer sees only their own orders,
which status may follow which, claiming shipped trackers for the customer
-- lives in routers/shop.py, once for both backends.
"""

from datetime import datetime, timezone
from typing import Protocol

from .schemas import (
    OrderCreate,
    OrderEventOut,
    OrderOut,
    ProductIn,
    ProductOut,
    ProductUpdate,
)


class ShopRepository(Protocol):
    async def list_products(self, include_inactive: bool) -> list[ProductOut]:
        """Cheapest first."""
        ...

    async def get_product(self, product_id: int) -> ProductOut | None: ...

    async def create_product(self, payload: ProductIn) -> ProductOut: ...

    async def update_product(
        self, product_id: int, payload: ProductUpdate
    ) -> ProductOut | None: ...

    async def create_order(
        self, user_id: int, username: str, product: ProductOut, payload: OrderCreate
    ) -> OrderOut:
        """At the product's current name and price, status `placed`."""
        ...

    async def list_orders(
        self, user_id: int | None = None, status: str | None = None, limit: int = 200
    ) -> list[OrderOut]:
        """Newest first. `user_id` None means every customer's."""
        ...

    async def get_order(self, order_id: int) -> OrderOut | None: ...

    async def update_order(
        self,
        order_id: int,
        changes: dict,
        changed_by: int | None,
        changed_by_name: str | None,
        note: str | None = None,
    ) -> OrderOut | None:
        """Apply `changes` (status, tracking, admin_note, device_ids). A
        status in it that differs from the current one adds a timeline
        event carrying `note`."""
        ...


def _total(unit_price: float, quantity: int) -> float:
    return round(unit_price * quantity, 2)


class InMemoryShopRepository:
    def __init__(self) -> None:
        self._products: dict[int, ProductOut] = {}
        self._orders: dict[int, OrderOut] = {}
        self._next_product = 1
        self._next_order = 1

    async def list_products(self, include_inactive: bool) -> list[ProductOut]:
        rows = [p for p in self._products.values() if include_inactive or p.active]
        return sorted(rows, key=lambda p: (p.price, p.id))

    async def get_product(self, product_id: int) -> ProductOut | None:
        return self._products.get(product_id)

    async def create_product(self, payload: ProductIn) -> ProductOut:
        product = ProductOut(
            id=self._next_product, created_at=datetime.now(timezone.utc), **payload.model_dump()
        )
        self._products[product.id] = product
        self._next_product += 1
        return product

    async def update_product(self, product_id, payload) -> ProductOut | None:
        current = self._products.get(product_id)
        if current is None:
            return None
        updated = current.model_copy(update=payload.model_dump(exclude_unset=True))
        self._products[product_id] = updated
        return updated

    async def create_order(self, user_id, username, product, payload) -> OrderOut:
        now = datetime.now(timezone.utc)
        order = OrderOut(
            id=self._next_order,
            user_id=user_id,
            username=username,
            product_id=product.id,
            product_name=product.name,
            unit_price=product.price,
            total=_total(product.price, payload.quantity),
            status="placed",
            created_at=now,
            updated_at=now,
            events=[OrderEventOut(status="placed", changed_by=username, changed_at=now)],
            **payload.model_dump(exclude={"product_id"}),
        )
        self._orders[order.id] = order
        self._next_order += 1
        return order

    async def list_orders(self, user_id=None, status=None, limit=200) -> list[OrderOut]:
        rows = [
            o
            for o in self._orders.values()
            if (user_id is None or o.user_id == user_id) and (status is None or o.status == status)
        ]
        return sorted(rows, key=lambda o: (o.created_at, o.id), reverse=True)[:limit]

    async def get_order(self, order_id: int) -> OrderOut | None:
        return self._orders.get(order_id)

    async def update_order(
        self, order_id, changes, changed_by, changed_by_name, note=None
    ) -> OrderOut | None:
        current = self._orders.get(order_id)
        if current is None:
            return None
        now = datetime.now(timezone.utc)
        events = list(current.events)
        if "status" in changes and changes["status"] != current.status:
            events.append(
                OrderEventOut(
                    status=changes["status"], note=note, changed_by=changed_by_name, changed_at=now
                )
            )
        updated = current.model_copy(update={**changes, "events": events, "updated_at": now})
        self._orders[order_id] = updated
        return updated


_PRODUCT_COLUMNS = "id, name, description, price::float8 AS price, active, created_at"

_ORDER_COLUMNS = """
    o.id, o.user_id, u.username, o.product_id, o.product_name,
    o.unit_price::float8 AS unit_price, o.quantity,
    (o.unit_price * o.quantity)::float8 AS total,
    o.contact_name, o.phone, o.address, o.city, o.state, o.pincode, o.notes,
    o.payment_method, o.status, o.tracking, o.admin_note, o.device_ids,
    o.created_at, o.updated_at
"""

# Columns update_order may set -- also the guard that keeps `changes` keys
# out of the SQL text unless they are one of these.
_UPDATABLE = ("status", "tracking", "admin_note", "device_ids")


class PostgresShopRepository:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def list_products(self, include_inactive: bool) -> list[ProductOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_PRODUCT_COLUMNS} FROM device_products
                WHERE active OR $1
                ORDER BY price, id
                """,
                include_inactive,
            )
        return [ProductOut(**dict(r)) for r in rows]

    async def get_product(self, product_id: int) -> ProductOut | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PRODUCT_COLUMNS} FROM device_products WHERE id = $1", product_id
            )
        return ProductOut(**dict(row)) if row else None

    async def create_product(self, payload: ProductIn) -> ProductOut:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO device_products (name, description, price, active)
                VALUES ($1, $2, $3, $4)
                RETURNING {_PRODUCT_COLUMNS}
                """,
                payload.name,
                payload.description,
                payload.price,
                payload.active,
            )
        return ProductOut(**dict(row))

    async def update_product(self, product_id, payload) -> ProductOut | None:
        sent = payload.model_dump(exclude_unset=True)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE device_products SET
                    name = CASE WHEN $2 THEN $3 ELSE name END,
                    description = CASE WHEN $4 THEN $5 ELSE description END,
                    price = CASE WHEN $6 THEN $7::numeric ELSE price END,
                    active = CASE WHEN $8 THEN $9 ELSE active END
                WHERE id = $1
                RETURNING {_PRODUCT_COLUMNS}
                """,
                product_id,
                "name" in sent,
                sent.get("name"),
                "description" in sent,
                sent.get("description"),
                "price" in sent,
                sent.get("price"),
                "active" in sent,
                sent.get("active"),
            )
        return ProductOut(**dict(row)) if row else None

    async def create_order(self, user_id, username, product, payload) -> OrderOut:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                order_id = await conn.fetchval(
                    """
                    INSERT INTO device_orders (
                        user_id, product_id, product_name, unit_price, quantity,
                        contact_name, phone, address, city, state, pincode, notes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    RETURNING id
                    """,
                    user_id,
                    product.id,
                    product.name,
                    product.price,
                    payload.quantity,
                    payload.contact_name,
                    payload.phone,
                    payload.address,
                    payload.city,
                    payload.state,
                    payload.pincode,
                    payload.notes,
                )
                await conn.execute(
                    """
                    INSERT INTO device_order_events (order_id, status, changed_by)
                    VALUES ($1, 'placed', $2)
                    """,
                    order_id,
                    user_id,
                )
            return await self._fetch_one(conn, order_id)

    async def list_orders(self, user_id=None, status=None, limit=200) -> list[OrderOut]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_ORDER_COLUMNS}
                FROM device_orders o LEFT JOIN users u ON u.id = o.user_id
                WHERE ($1::bigint IS NULL OR o.user_id = $1)
                  AND ($2::text IS NULL OR o.status = $2)
                ORDER BY o.created_at DESC, o.id DESC
                LIMIT $3
                """,
                user_id,
                status,
                limit,
            )
            events = await self._events(conn, [r["id"] for r in rows])
        return [OrderOut(**dict(r), events=events.get(r["id"], [])) for r in rows]

    async def get_order(self, order_id: int) -> OrderOut | None:
        async with self._pool.acquire() as conn:
            return await self._fetch_one(conn, order_id)

    async def update_order(
        self, order_id, changes, changed_by, changed_by_name, note=None
    ) -> OrderOut | None:
        fields = [k for k in _UPDATABLE if k in changes]
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchval(
                    "SELECT status FROM device_orders WHERE id = $1 FOR UPDATE", order_id
                )
                if previous is None:
                    return None
                assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(fields))
                await conn.execute(
                    f"""
                    UPDATE device_orders
                    SET {assignments + ", " if assignments else ""}updated_at = now()
                    WHERE id = $1
                    """,
                    order_id,
                    *(changes[name] for name in fields),
                )
                if "status" in changes and changes["status"] != previous:
                    await conn.execute(
                        """
                        INSERT INTO device_order_events (order_id, status, note, changed_by)
                        VALUES ($1, $2, $3, $4)
                        """,
                        order_id,
                        changes["status"],
                        note,
                        changed_by,
                    )
            return await self._fetch_one(conn, order_id)

    async def _fetch_one(self, conn, order_id: int) -> OrderOut | None:
        row = await conn.fetchrow(
            f"""
            SELECT {_ORDER_COLUMNS}
            FROM device_orders o LEFT JOIN users u ON u.id = o.user_id
            WHERE o.id = $1
            """,
            order_id,
        )
        if row is None:
            return None
        events = await self._events(conn, [order_id])
        return OrderOut(**dict(row), events=events.get(order_id, []))

    async def _events(self, conn, order_ids: list[int]) -> dict[int, list[OrderEventOut]]:
        if not order_ids:
            return {}
        rows = await conn.fetch(
            """
            SELECT e.order_id, e.status, e.note, u.username AS changed_by, e.changed_at
            FROM device_order_events e LEFT JOIN users u ON u.id = e.changed_by
            WHERE e.order_id = ANY($1::bigint[])
            ORDER BY e.changed_at, e.id
            """,
            order_ids,
        )
        out: dict[int, list[OrderEventOut]] = {}
        for r in rows:
            data = dict(r)
            out.setdefault(data.pop("order_id"), []).append(OrderEventOut(**data))
        return out
