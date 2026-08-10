"""PostgreSQL integration tests for trades inventory foundations."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable

import pytest
from sqlalchemy import select

from app.api.internal_schemas import (
    CreditInventoryItemRequest,
    CreditInventoryRequest,
    ReleaseInventoryRequest,
    ReservationItemRequest,
    ReserveInventoryRequest,
)
from app.models.inventory import (
    InventoryOperation,
    SyncStatusEnum,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services import reconciler
from app.services.cardtrader_client import CardTraderAPIError
from app.services.inventory_operations import (
    InventoryOperationError,
    consume_inventory,
    credit_inventory,
    recover_stale_releases,
    recover_stale_reservations,
    release_inventory,
    reserve_inventory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class FakeCardTraderClient:
    def __init__(
        self,
        products: Dict[int, int],
        *,
        fail_negative_for: Iterable[int] = (),
        fail_positive_after_apply_for: Iterable[int] = (),
    ) -> None:
        self.products = products
        self.fail_negative_for = set(fail_negative_for)
        self.fail_positive_after_apply_for = set(fail_positive_after_apply_for)
        self.calls: list[tuple[int, int]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.product_payloads: dict[int, dict[str, Any]] = {}
        self.next_product_id = max(products, default=0) + 1000
        self.export_calls = 0

    async def __aenter__(self) -> "FakeCardTraderClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get_products_export(self) -> list[dict[str, Any]]:
        self.export_calls += 1
        return [
            {
                "id": product_id,
                "game_id": 1,
                "blueprint_id": self.product_payloads.get(product_id, {}).get(
                    "blueprint_id", product_id
                ),
                "quantity": quantity,
                "price_cents": int(
                    float(self.product_payloads.get(product_id, {}).get("price", 12.34))
                    * 100
                ),
                "user_data_field": self.product_payloads.get(product_id, {}).get(
                    "user_data_field"
                ),
            }
            for product_id, quantity in self.products.items()
        ]

    async def create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(dict(payload))
        product_id = self.next_product_id
        self.next_product_id += 1
        self.products[product_id] = int(payload["quantity"])
        self.product_payloads[product_id] = dict(payload)
        return {
            "result": "ok",
            "resource": {
                "id": product_id,
                "blueprint_id": int(payload["blueprint_id"]),
                "quantity": int(payload["quantity"]),
                "user_data_field": payload.get("user_data_field"),
            },
        }

    async def increment_product_quantity(
        self, product_id: int, delta_quantity: int
    ) -> dict[str, int]:
        self.calls.append((product_id, delta_quantity))
        if delta_quantity < 0 and product_id in self.fail_negative_for:
            raise RuntimeError("CardTrader test failure")
        if product_id not in self.products:
            raise CardTraderAPIError("CardTrader product missing", status_code=404)
        new_quantity = self.products[product_id] + delta_quantity
        if new_quantity <= 0:
            del self.products[product_id]
            return {"quantity": 0}
        self.products[product_id] = new_quantity
        if delta_quantity > 0 and product_id in self.fail_positive_after_apply_for:
            raise RuntimeError("CardTrader response lost after applied increment")
        return {"quantity": new_quantity}


def client_factory(client: FakeCardTraderClient):
    return lambda _token, _user_id: client


def real_sync_settings(user_id: uuid.UUID) -> UserSyncSettings:
    return UserSyncSettings(
        user_id=user_id,
        cardtrader_token_encrypted="encrypted",
        sync_status=SyncStatusEnum.ACTIVE.value,
        execution_mode="real",
        writes_enabled=True,
    )


async def add_inventory_item(
    session,
    *,
    user_id: uuid.UUID,
    quantity: int,
    source: str,
    external_stock_id: str | None = None,
    blueprint_id: int = 100,
    environment: str = "real",
) -> UserInventoryItem:
    item = UserInventoryItem(
        user_id=user_id,
        blueprint_id=blueprint_id,
        game_id=1 if source == "cardtrader" else None,
        quantity=quantity,
        price_cents=1234,
        properties={"condition": "Near Mint"},
        external_stock_id=external_stock_id,
        source=source,
        environment=environment,
        description="snapshot",
        graded=False,
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.asyncio
async def test_concurrent_reservations_only_one_wins(test_session_factory):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(real_sync_settings(user_id))
            item = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="11",
            )
            item_id = item.id
    fake = FakeCardTraderClient({11: 1})

    async def attempt(op_key: str):
        async with test_session_factory() as session:
            return await reserve_inventory(
                session,
                ReserveInventoryRequest(
                    op_key=op_key,
                    user_id=user_id,
                    items=[ReservationItemRequest(item_id=item_id, quantity=1)],
                ),
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )

    results = await asyncio.gather(
        attempt("trade:1:reserve"),
        attempt("trade:2:reserve"),
        return_exceptions=True,
    )

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], InventoryOperationError)
    assert errors[0].code == "INVENTORY_UNAVAILABLE"

    async with test_session_factory() as session:
        stored = await session.get(UserInventoryItem, item_id)
        assert stored is not None
        assert stored.quantity == 0
        assert stored.reserved_quantity == 1


@pytest.mark.asyncio
async def test_reservation_is_blocked_before_stock_changes_outside_real(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(
                UserSyncSettings(
                    user_id=user_id,
                    cardtrader_token_encrypted="encrypted",
                    sync_status=SyncStatusEnum.ACTIVE.value,
                    execution_mode="partial",
                    writes_enabled=False,
                )
            )
            item = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="12",
                environment="partial",
            )
            item_id = item.id

    fake = FakeCardTraderClient({12: 1})
    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError) as blocked:
            await reserve_inventory(
                session,
                ReserveInventoryRequest(
                    op_key="trade:policy:reserve",
                    user_id=user_id,
                    items=[ReservationItemRequest(item_id=item_id, quantity=1)],
                ),
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )

    assert blocked.value.code == "CARDTRADER_WRITES_BLOCKED"
    assert fake.calls == []
    async with test_session_factory() as session:
        stored = await session.get(UserInventoryItem, item_id)
        assert stored is not None
        assert (stored.quantity, stored.reserved_quantity) == (1, 0)


@pytest.mark.asyncio
async def test_cardtrader_mid_batch_failure_stays_locked_and_recovers_forward(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(real_sync_settings(user_id))
            first = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="101",
                blueprint_id=101,
            )
            second = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="202",
                blueprint_id=202,
            )
            item_ids = (first.id, second.id)

    fake = FakeCardTraderClient({101: 2, 202: 2}, fail_negative_for={202})
    request = ReserveInventoryRequest(
        op_key="trade:3:reserve",
        user_id=user_id,
        items=[
            ReservationItemRequest(item_id=item_ids[0], quantity=1),
            ReservationItemRequest(item_id=item_ids[1], quantity=1),
        ],
    )

    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError) as failed:
            await reserve_inventory(
                session,
                request,
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )
    assert failed.value.code == "CARDTRADER_RESERVATION_PENDING"
    assert fake.products == {101: 1, 202: 2}
    calls_after_failure = list(fake.calls)

    async with test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(UserInventoryItem)
                    .where(UserInventoryItem.id.in_(item_ids))
                    .order_by(UserInventoryItem.id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.quantity for row in rows] == [1, 0]
        assert [row.reserved_quantity for row in rows] == [1, 1]
        operation = (
            await session.execute(
                select(InventoryOperation).where(InventoryOperation.op_key == request.op_key)
            )
        ).scalar_one()
        assert operation.status == "processing"

    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError) as replay:
            await reserve_inventory(
                session,
                request,
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )
    assert replay.value.code == "OPERATION_IN_PROGRESS"
    assert fake.calls == calls_after_failure

    fake.fail_negative_for.clear()
    async with test_session_factory() as session:
        recovered = await recover_stale_reservations(
            session,
            stale_minutes=0,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    assert recovered == {"scanned": 1, "recovered": 0, "pending": 0, "ambiguous": 1}
    assert fake.products == {101: 1, 202: 2}
    assert fake.calls == calls_after_failure


@pytest.mark.asyncio
async def test_deleted_product_is_recreated_when_release_restores_stock(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(real_sync_settings(user_id))
            item = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="303",
            )
            item_id = item.id

    fake = FakeCardTraderClient({303: 1})
    reserve_request = ReserveInventoryRequest(
        op_key="trade:4:reserve",
        user_id=user_id,
        items=[ReservationItemRequest(item_id=item_id, quantity=1)],
    )
    async with test_session_factory() as session:
        reserved = await reserve_inventory(
            session,
            reserve_request,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    assert fake.products == {}
    calls_after_reserve = list(fake.calls)
    async with test_session_factory() as session:
        reserve_replay = await reserve_inventory(
            session,
            reserve_request,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    assert reserve_replay["replayed"] is True
    assert reserve_replay["items"][0]["item_id"] == reserved["items"][0]["item_id"]
    assert fake.calls == calls_after_reserve

    release_request = ReleaseInventoryRequest(
        op_key="trade:4:release",
        reservation_op_key=reserve_request.op_key,
        user_id=user_id,
    )
    async with test_session_factory() as session:
        released = await release_inventory(
            session,
            release_request,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    assert released["status"] == "succeeded"
    assert len(fake.create_calls) == 1
    recreated_id = next(iter(fake.products))
    assert recreated_id != 303
    assert fake.products[recreated_id] == 1

    async with test_session_factory() as session:
        replay = await release_inventory(
            session,
            release_request,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    assert replay["replayed"] is True
    assert len(fake.create_calls) == 1

    async with test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(UserInventoryItem)
                    .where(UserInventoryItem.user_id == user_id)
                    .order_by(UserInventoryItem.id)
                )
            )
            .scalars()
            .all()
        )
        operation = (
            await session.execute(
                select(InventoryOperation).where(
                    InventoryOperation.op_key == release_request.op_key
                )
            )
        ).scalar_one()
        assert [
            (
                row.source,
                row.quantity,
                row.reserved_quantity,
                row.external_stock_id,
                row.user_data_field,
            )
            for row in rows
        ] == [
            (
                "cardtrader",
                1,
                0,
                str(recreated_id),
                f"ebartex_inventory:{user_id}:{item_id}",
            ),
        ]
        assert operation.status == "succeeded"


@pytest.mark.asyncio
async def test_ambiguous_cardtrader_quantity_never_reopens_stock(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(real_sync_settings(user_id))
            item = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=2,
                source="cardtrader",
                external_stock_id="350",
            )
            item_id = item.id

    fake = FakeCardTraderClient({350: 2}, fail_negative_for={350})
    request = ReserveInventoryRequest(
        op_key="trade:ambiguous:reserve",
        user_id=user_id,
        items=[ReservationItemRequest(item_id=item_id, quantity=2)],
    )
    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError) as pending:
            await reserve_inventory(
                session,
                request,
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )
    assert pending.value.code == "CARDTRADER_RESERVATION_PENDING"

    # A concurrent CardTrader sale moved stock to neither the before nor the
    # expected after value. Recovery must not guess which mutation landed.
    fake.fail_negative_for.clear()
    fake.products[350] = 1
    async with test_session_factory() as session:
        recovery = await recover_stale_reservations(
            session,
            stale_minutes=0,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    assert recovery == {"scanned": 1, "recovered": 0, "pending": 0, "ambiguous": 1}

    async with test_session_factory() as session:
        stored_item = await session.get(UserInventoryItem, item_id)
        operation = (
            await session.execute(
                select(InventoryOperation).where(InventoryOperation.op_key == request.op_key)
            )
        ).scalar_one()
        assert stored_item is not None
        assert stored_item.quantity == 0
        assert stored_item.reserved_quantity == 2
        assert operation.status == "processing"
        assert operation.result_json["phase"] == "manual_review_required"


@pytest.mark.asyncio
async def test_reserve_recovery_marker_race_requires_manual_review(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(real_sync_settings(user_id))
            item = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="360",
            )
            item_id = item.id

    fake = FakeCardTraderClient({360: 1}, fail_negative_for={360})
    request = ReserveInventoryRequest(
        op_key="trade:reserve-marker-race",
        user_id=user_id,
        items=[ReservationItemRequest(item_id=item_id, quantity=1)],
    )
    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError):
            await reserve_inventory(
                session,
                request,
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )

    fake.products.pop(360)
    fake.fail_negative_for.clear()
    async with test_session_factory() as session:
        async with session.begin():
            stored_item = await session.get(UserInventoryItem, item_id)
            stored_item.sync_uncertain_event_id = 90

    async with test_session_factory() as session:
        recovery = await recover_stale_reservations(
            session,
            stale_minutes=0,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )

    assert recovery == {
        "scanned": 1,
        "recovered": 0,
        "pending": 0,
        "ambiguous": 1,
    }
    async with test_session_factory() as session:
        operation = (
            await session.execute(
                select(InventoryOperation).where(InventoryOperation.op_key == request.op_key)
            )
        ).scalar_one()
        stored_item = await session.get(UserInventoryItem, item_id)
        assert operation.result_json["phase"] == "manual_review_required"
        assert stored_item.sync_uncertain_event_id == 90
        assert stored_item.sync_state == "pending"


@pytest.mark.asyncio
async def test_release_recovery_marker_race_requires_manual_review(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(real_sync_settings(user_id))
            item = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=2,
                source="cardtrader",
                external_stock_id="370",
            )
            item_id = item.id

    fake = FakeCardTraderClient({370: 2})
    reserve_request = ReserveInventoryRequest(
        op_key="trade:release-marker-race:reserve",
        user_id=user_id,
        items=[ReservationItemRequest(item_id=item_id, quantity=1)],
    )
    async with test_session_factory() as session:
        await reserve_inventory(
            session,
            reserve_request,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )

    fake.fail_positive_after_apply_for.add(370)
    release_request = ReleaseInventoryRequest(
        op_key="trade:release-marker-race",
        reservation_op_key=reserve_request.op_key,
        user_id=user_id,
    )
    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError):
            await release_inventory(
                session,
                release_request,
                client_factory=client_factory(fake),
                decrypt_token=lambda value: value,
            )

    fake.fail_positive_after_apply_for.clear()
    async with test_session_factory() as session:
        async with session.begin():
            stored_item = await session.get(UserInventoryItem, item_id)
            stored_item.sync_uncertain_event_id = 91

    async with test_session_factory() as session:
        recovery = await recover_stale_releases(
            session,
            stale_minutes=0,
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )

    assert recovery == {
        "scanned": 1,
        "recovered": 0,
        "pending": 0,
        "ambiguous": 1,
    }
    async with test_session_factory() as session:
        operation = (
            await session.execute(
                select(InventoryOperation).where(
                    InventoryOperation.op_key == release_request.op_key
                )
            )
        ).scalar_one()
        stored_item = await session.get(UserInventoryItem, item_id)
        assert operation.result_json["phase"] == "release_manual_review_required"
        assert stored_item.sync_uncertain_event_id == 91
        assert stored_item.sync_state == "pending"


@pytest.mark.asyncio
async def test_credit_is_idempotent_without_sync_settings(test_session_factory):
    user_id = uuid.uuid4()
    request = CreditInventoryRequest(
        op_key="trade:5:credit",
        user_id=user_id,
        items=[
            CreditInventoryItemRequest(
                blueprint_id=500,
                quantity=2,
                price_cents=999,
                properties={"condition": "Excellent"},
            )
        ],
    )
    async with test_session_factory() as session:
        first = await credit_inventory(session, request)
    async with test_session_factory() as session:
        replay = await credit_inventory(session, request)

    assert first["items"][0]["target_item_id"] == replay["items"][0]["target_item_id"]
    assert replay["replayed"] is True
    async with test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].source == "trade"
        assert rows[0].external_stock_id is None


@pytest.mark.asyncio
async def test_two_way_trade_transfer_preserves_inventory_totals(test_session_factory):
    proposer_id = uuid.uuid4()
    receiver_id = uuid.uuid4()
    op_suffix = uuid.uuid4().hex

    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add_all(
                [
                    real_sync_settings(proposer_id),
                    real_sync_settings(receiver_id),
                ]
            )
            offered = await add_inventory_item(
                setup_session,
                user_id=proposer_id,
                quantity=2,
                source="cardtrader",
                external_stock_id="70101",
                blueprint_id=701,
            )
            requested = await add_inventory_item(
                setup_session,
                user_id=receiver_id,
                quantity=3,
                source="cardtrader",
                external_stock_id="70202",
                blueprint_id=702,
            )
            offered_id = offered.id
            requested_id = requested.id
    fake = FakeCardTraderClient({70101: 2, 70202: 3})

    async with test_session_factory() as session:
        proposer_reservation = await reserve_inventory(
            session,
            ReserveInventoryRequest(
                op_key=f"trade:{op_suffix}:reserve:proposer",
                user_id=proposer_id,
                items=[ReservationItemRequest(item_id=offered_id, quantity=1)],
            ),
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )
    async with test_session_factory() as session:
        receiver_reservation = await reserve_inventory(
            session,
            ReserveInventoryRequest(
                op_key=f"trade:{op_suffix}:reserve:receiver",
                user_id=receiver_id,
                items=[ReservationItemRequest(item_id=requested_id, quantity=2)],
            ),
            client_factory=client_factory(fake),
            decrypt_token=lambda value: value,
        )

    offered_snapshot = proposer_reservation["items"][0]
    requested_snapshot = receiver_reservation["items"][0]
    proposer_credit = CreditInventoryRequest(
        op_key=f"trade:{op_suffix}:credit:proposer",
        user_id=proposer_id,
        items=[
            CreditInventoryItemRequest(
                blueprint_id=requested_snapshot["blueprint_id"],
                quantity=requested_snapshot["quantity"],
                price_cents=requested_snapshot["price_cents"],
                properties=requested_snapshot["properties"],
                description=requested_snapshot["description"],
                graded=requested_snapshot["graded"],
            )
        ],
    )
    receiver_credit = CreditInventoryRequest(
        op_key=f"trade:{op_suffix}:credit:receiver",
        user_id=receiver_id,
        items=[
            CreditInventoryItemRequest(
                blueprint_id=offered_snapshot["blueprint_id"],
                quantity=offered_snapshot["quantity"],
                price_cents=offered_snapshot["price_cents"],
                properties=offered_snapshot["properties"],
                description=offered_snapshot["description"],
                graded=offered_snapshot["graded"],
            )
        ],
    )

    async with test_session_factory() as session:
        await credit_inventory(session, proposer_credit)
    async with test_session_factory() as session:
        await credit_inventory(session, receiver_credit)
    async with test_session_factory() as session:
        await consume_inventory(
            session,
            ReleaseInventoryRequest(
                op_key=f"trade:{op_suffix}:consume:proposer",
                reservation_op_key=f"trade:{op_suffix}:reserve:proposer",
                user_id=proposer_id,
            ),
        )
    async with test_session_factory() as session:
        await consume_inventory(
            session,
            ReleaseInventoryRequest(
                op_key=f"trade:{op_suffix}:consume:receiver",
                reservation_op_key=f"trade:{op_suffix}:reserve:receiver",
                user_id=receiver_id,
            ),
        )
    async with test_session_factory() as session:
        proposer_replay = await credit_inventory(session, proposer_credit)
        receiver_replay = await credit_inventory(session, receiver_credit)

    assert proposer_replay["replayed"] is True
    assert receiver_replay["replayed"] is True

    async with test_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(UserInventoryItem).where(
                        UserInventoryItem.user_id.in_([proposer_id, receiver_id])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert sum(row.quantity for row in rows) == 5
    assert all(row.quantity >= 0 for row in rows)
    assert all(row.reserved_quantity == 0 for row in rows)
    credited_rows = [row for row in rows if row.id not in {offered_id, requested_id}]
    assert len(credited_rows) == 2
    assert all(row.source == "trade" and row.external_stock_id is None for row in credited_rows)
    proposer_received = next(row for row in credited_rows if row.user_id == proposer_id)

    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError) as not_published:
            await reserve_inventory(
                session,
                ReserveInventoryRequest(
                    op_key=f"trade:{op_suffix}:reserve:received-unlisted",
                    user_id=proposer_id,
                    items=[
                        ReservationItemRequest(
                            item_id=proposer_received.id,
                            quantity=1,
                        )
                    ],
                ),
            )
    assert not_published.value.code == "INVENTORY_UNAVAILABLE"


class FakeMutationLease:
    def refresh(self) -> None:
        return None


@pytest.mark.asyncio
async def test_reconciler_does_not_change_active_escrow_or_trade_rows(
    test_session_factory, monkeypatch
):
    user_id = uuid.uuid4()
    async with test_session_factory() as setup_session:
        async with setup_session.begin():
            settings = UserSyncSettings(
                user_id=user_id,
                cardtrader_token_encrypted="encrypted",
                sync_status=SyncStatusEnum.ACTIVE.value,
                execution_mode="partial",
            )
            setup_session.add(settings)
            linked = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=1,
                source="cardtrader",
                external_stock_id="606",
                blueprint_id=606,
                environment="partial",
            )
            internal = await add_inventory_item(
                setup_session,
                user_id=user_id,
                quantity=3,
                source="trade",
                blueprint_id=607,
            )
            linked_id, internal_id = linked.id, internal.id

    async def load_aligned_inventory(session, _settings):
        linked_row = await session.get(UserInventoryItem, linked_id)
        await session.commit()
        return (
            [linked_row],
            1,
            [
                {
                    "id": 606,
                    "game_id": 1,
                    "blueprint_id": 606,
                    "quantity": 1,
                    "price_cents": 1234,
                }
            ],
        )

    @asynccontextmanager
    async def fake_mutation_lease(_user_id):
        yield FakeMutationLease()

    monkeypatch.setattr(reconciler, "_load_local_and_export", load_aligned_inventory)
    monkeypatch.setattr(
        reconciler,
        "cardtrader_mutation_lease",
        fake_mutation_lease,
    )

    async with test_session_factory() as session:
        sync_settings = await session.get(UserSyncSettings, user_id)
        result = await reconciler.reconcile_user_apply(
            session,
            sync_settings,
            lambda blueprint_id: (blueprint_id, "cards_prints"),
        )
        assert result["applied"] == {
            "updated": 1,
            "created": 0,
            "sold_out": 0,
            "missing_quarantined": 0,
            "archived": 0,
            "skipped_unsafe": 0,
            "skipped_zero_qty": 0,
            "unsupported_export_rows": 0,
            "legacy_non_magic_quarantined": 0,
        }

    async with test_session_factory() as session:
        linked_row = await session.get(UserInventoryItem, linked_id)
        internal_row = await session.get(UserInventoryItem, internal_id)
        assert linked_row.quantity == 1
        assert internal_row.quantity == 3
