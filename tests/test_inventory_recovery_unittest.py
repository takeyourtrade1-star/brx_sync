"""Stdlib integration tests for safety-critical inventory recovery.

These intentionally avoid pytest-only fixtures so they can also run inside the
production-like image against an explicitly disposable PostgreSQL database.
"""

import os
import unittest
import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.internal_schemas import (
    ReleaseInventoryRequest,
    ReservationItemRequest,
    ReserveInventoryRequest,
)
from app.models.inventory import (
    Base,
    InventoryOperation,
    SyncStatusEnum,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services.cardtrader_client import CardTraderAPIError
from app.services.inventory_operations import (
    InventoryOperationError,
    recover_stale_releases,
    recover_stale_reservations,
    release_inventory,
    reserve_inventory,
)


class FakeCardTraderClient:
    def __init__(
        self,
        products: dict[int, int],
        *,
        fail_negative_for: Iterable[int] = (),
    ) -> None:
        self.products = products
        self.fail_negative_for = set(fail_negative_for)

    async def __aenter__(self) -> "FakeCardTraderClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get_products_export(self) -> list[dict[str, int]]:
        return [
            {"id": product_id, "quantity": quantity}
            for product_id, quantity in self.products.items()
        ]

    async def increment_product_quantity(
        self,
        product_id: int,
        delta_quantity: int,
    ) -> dict[str, Any]:
        if delta_quantity < 0 and product_id in self.fail_negative_for:
            raise RuntimeError("simulated unknown mutation outcome")
        if product_id not in self.products:
            raise CardTraderAPIError("missing", status_code=404)
        new_quantity = self.products[product_id] + delta_quantity
        if new_quantity <= 0:
            del self.products[product_id]
            return {"result": "ok", "resource": {"quantity": 0}}
        self.products[product_id] = new_quantity
        return {"result": "ok", "resource": {"quantity": new_quantity}}


def _client_factory(client: FakeCardTraderClient):
    return lambda _token, _user_id: client


class InventoryRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        database_url = os.environ.get("TEST_DATABASE_URL")
        if not database_url:
            self.skipTest("TEST_DATABASE_URL is required")
        self.engine = create_async_engine(database_url, pool_pre_ping=True)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def asyncTearDown(self) -> None:
        if not hasattr(self, "engine"):
            return
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()

    async def _seed(self, external_stock_id: str) -> tuple[uuid.UUID, int]:
        user_id = uuid.uuid4()
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    UserSyncSettings(
                        user_id=user_id,
                        cardtrader_token_encrypted="encrypted",
                        sync_status=SyncStatusEnum.ACTIVE.value,
                    )
                )
                item = UserInventoryItem(
                    user_id=user_id,
                    blueprint_id=100,
                    quantity=1,
                    price_cents=1000,
                    properties={"condition": "Near Mint"},
                    external_stock_id=external_stock_id,
                    source="cardtrader",
                )
                session.add(item)
                await session.flush()
                return user_id, item.id

    async def test_unknown_reserve_outcome_stays_locked_then_recovers(self) -> None:
        user_id, item_id = await self._seed("601")
        fake = FakeCardTraderClient({601: 1}, fail_negative_for={601})
        request = ReserveInventoryRequest(
            op_key="trade:unittest:reserve",
            user_id=user_id,
            items=[ReservationItemRequest(item_id=item_id, quantity=1)],
        )

        async with self.sessions() as session:
            with self.assertRaises(InventoryOperationError) as pending:
                await reserve_inventory(
                    session,
                    request,
                    client_factory=_client_factory(fake),
                    decrypt_token=lambda value: value,
                )
        self.assertEqual(pending.exception.code, "CARDTRADER_RESERVATION_PENDING")

        async with self.sessions() as session:
            item = await session.get(UserInventoryItem, item_id)
            self.assertEqual((item.quantity, item.reserved_quantity), (0, 1))

        fake.fail_negative_for.clear()
        async with self.sessions() as session:
            result = await recover_stale_reservations(
                session,
                stale_minutes=0,
                client_factory=_client_factory(fake),
                decrypt_token=lambda value: value,
            )
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(fake.products, {})

    async def test_missing_product_release_falls_back_without_duplicate(self) -> None:
        user_id, item_id = await self._seed("701")
        fake = FakeCardTraderClient({701: 1})
        reserve_request = ReserveInventoryRequest(
            op_key="trade:unittest:reserve-for-release",
            user_id=user_id,
            items=[ReservationItemRequest(item_id=item_id, quantity=1)],
        )
        async with self.sessions() as session:
            await reserve_inventory(
                session,
                reserve_request,
                client_factory=_client_factory(fake),
                decrypt_token=lambda value: value,
            )
        self.assertEqual(fake.products, {})

        release_request = ReleaseInventoryRequest(
            op_key="trade:unittest:release",
            reservation_op_key=reserve_request.op_key,
            user_id=user_id,
        )
        async with self.sessions() as session:
            with self.assertRaises(InventoryOperationError) as pending:
                await release_inventory(
                    session,
                    release_request,
                    client_factory=_client_factory(fake),
                    decrypt_token=lambda value: value,
                )
        self.assertEqual(pending.exception.code, "CARDTRADER_RELEASE_PENDING")

        async with self.sessions() as session:
            result = await recover_stale_releases(
                session,
                stale_minutes=0,
                client_factory=_client_factory(fake),
                decrypt_token=lambda value: value,
            )
        self.assertEqual(result["recovered"], 1)

        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(UserInventoryItem)
                    .where(UserInventoryItem.user_id == user_id)
                    .order_by(UserInventoryItem.id)
                )
            ).scalars().all()
            operation = (
                await session.execute(
                    select(InventoryOperation).where(
                        InventoryOperation.op_key == release_request.op_key
                    )
                )
            ).scalar_one()
        self.assertEqual(
            [(row.source, row.quantity, row.reserved_quantity) for row in rows],
            [("cardtrader", 0, 0), ("trade", 1, 0)],
        )
        self.assertEqual(operation.status, "succeeded")
