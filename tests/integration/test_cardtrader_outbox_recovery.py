import uuid
from contextlib import asynccontextmanager

import pytest

from app.models.inventory import CardTraderOutbox, UserInventoryItem
from app.tasks import outbox_tasks

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _isolated_sessions(session_factory):
    @asynccontextmanager
    async def isolated():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return isolated


async def _seed_uncertain(
    session_factory,
    *,
    payload,
    marker=None,
):
    user_id = uuid.uuid4()
    command_id = uuid.uuid4()
    async with session_factory() as session:
        item = UserInventoryItem(
            user_id=user_id,
            blueprint_id=100,
            game_id=1,
            quantity=payload.get("quantity", 2),
            reserved_quantity=0,
            price_cents=350,
            properties={"condition": "Near Mint"},
            external_stock_id="123",
            source="cardtrader",
            environment="real",
            lifecycle_status="active",
            sync_state="uncertain",
            sync_uncertain_event_id=marker,
            row_version=2,
        )
        session.add(item)
        await session.flush()
        session.add(
            CardTraderOutbox(
                id=command_id,
                user_id=user_id,
                mode_version=1,
                operation_type="update_product",
                target_product_id="123",
                inventory_item_id=item.id,
                expected_row_version=2,
                payload_json=payload,
                status="uncertain",
            )
        )
        await session.commit()
        return command_id, item.id


@pytest.mark.asyncio
async def test_uncertain_matching_export_becomes_verified(
    test_session_factory,
    monkeypatch,
):
    command_id, item_id = await _seed_uncertain(
        test_session_factory,
        payload={"id": 123, "quantity": 2, "price": 3.5},
    )
    monkeypatch.setattr(
        outbox_tasks,
        "get_isolated_db_session",
        _isolated_sessions(test_session_factory),
    )

    result = await outbox_tasks.resolve_uncertain_command_from_export(
        command_id,
        {
            "id": "123",
            "game_id": 1,
            "blueprint_id": 100,
            "quantity": 2,
            "price_cents": 350,
        },
    )

    assert result == "verified"
    async with test_session_factory() as session:
        command = await session.get(CardTraderOutbox, command_id)
        item = await session.get(UserInventoryItem, item_id)
        assert command.status == "verified"
        assert item.sync_state == "synced"


@pytest.mark.asyncio
async def test_uncertain_mismatch_aligns_to_export_and_fails_command(
    test_session_factory,
    monkeypatch,
):
    command_id, item_id = await _seed_uncertain(
        test_session_factory,
        payload={"id": 123, "quantity": 2, "price": 3.5},
    )
    monkeypatch.setattr(
        outbox_tasks,
        "get_isolated_db_session",
        _isolated_sessions(test_session_factory),
    )

    result = await outbox_tasks.resolve_uncertain_command_from_export(
        command_id,
        {
            "id": "123",
            "game_id": 1,
            "blueprint_id": 100,
            "quantity": 1,
            "price_cents": 275,
        },
    )

    assert result == "failed"
    async with test_session_factory() as session:
        command = await session.get(CardTraderOutbox, command_id)
        item = await session.get(UserInventoryItem, item_id)
        assert command.status == "failed"
        assert item.quantity == 1
        assert item.price_cents == 275
        assert item.sync_state == "synced"


@pytest.mark.asyncio
async def test_webhook_marker_wins_resolver_cas(
    test_session_factory,
    monkeypatch,
):
    command_id, item_id = await _seed_uncertain(
        test_session_factory,
        payload={"id": 123, "quantity": 2, "price": 3.5},
        marker=77,
    )
    monkeypatch.setattr(
        outbox_tasks,
        "get_isolated_db_session",
        _isolated_sessions(test_session_factory),
    )

    result = await outbox_tasks.resolve_uncertain_command_from_export(
        command_id,
        {
            "id": "123",
            "game_id": 1,
            "blueprint_id": 100,
            "quantity": 2,
            "price_cents": 350,
        },
    )

    assert result == "deferred"
    async with test_session_factory() as session:
        command = await session.get(CardTraderOutbox, command_id)
        item = await session.get(UserInventoryItem, item_id)
        assert command.status == "uncertain"
        assert item.sync_state == "uncertain"
        assert item.sync_uncertain_event_id == 77


@pytest.mark.asyncio
async def test_zero_quantity_update_is_verified_when_product_is_absent(
    test_session_factory,
    monkeypatch,
):
    command_id, _ = await _seed_uncertain(
        test_session_factory,
        payload={"id": 123, "quantity": 0, "price": 3.5},
    )
    monkeypatch.setattr(
        outbox_tasks,
        "get_isolated_db_session",
        _isolated_sessions(test_session_factory),
    )

    result = await outbox_tasks.resolve_uncertain_command_from_export(
        command_id,
        None,
    )

    assert result == "verified"
