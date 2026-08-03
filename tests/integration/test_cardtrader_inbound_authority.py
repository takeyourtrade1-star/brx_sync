"""PostgreSQL regressions for inbound CardTrader watermark authority."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text, update

from app.models.inventory import (
    SyncOperation,
    SyncStatusEnum,
    UserInventoryItem,
    UserSyncSettings,
    WebhookInbox,
)
from app.services import reconciler
from app.services.marketplace_projection import project_inventory_to_marketplace
from app.services.webhook_ledger_processor import (
    WebhookLedgerProcessor,
    _quarantine_inventory,
)
from app.tasks import periodic_sync

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class _FakeMutationLease:
    def refresh(self) -> None:
        return None


@pytest.fixture(autouse=True)
def no_external_mutation_lease(monkeypatch):
    @asynccontextmanager
    async def lease(_user_id):
        yield _FakeMutationLease()

    monkeypatch.setattr(reconciler, "cardtrader_mutation_lease", lease)


def _settings(user_id: uuid.UUID) -> UserSyncSettings:
    return UserSyncSettings(
        user_id=user_id,
        cardtrader_token_encrypted="encrypted",
        sync_status=SyncStatusEnum.ACTIVE.value,
        execution_mode="partial",
        writes_enabled=False,
    )


def _item(
    user_id: uuid.UUID,
    *,
    product_id: str = "101",
    quantity: int = 5,
    reserved_quantity: int = 0,
    sync_state: str = "synced",
    marker: int | None = None,
) -> UserInventoryItem:
    return UserInventoryItem(
        user_id=user_id,
        blueprint_id=501,
        game_id=1,
        quantity=quantity,
        reserved_quantity=reserved_quantity,
        price_cents=250,
        properties={},
        external_stock_id=product_id,
        source="cardtrader",
        environment="partial",
        lifecycle_status="active",
        sync_state=sync_state,
        sync_uncertain_event_id=marker,
        row_version=4,
        mapping_status="mapped",
    )


def _inbox(user_id: uuid.UUID, webhook_id: str) -> WebhookInbox:
    return WebhookInbox(
        webhook_id=webhook_id,
        user_id=user_id,
        cause="order.update",
        mode="live",
        payload_json={},
        signature_valid=True,
        status="reconcile_pending",
    )


def _export(quantity: int) -> list[dict]:
    return [
        {
            "id": 101,
            "game_id": 1,
            "blueprint_id": 501,
            "quantity": quantity,
            "price_cents": 250,
            "properties_hash": {},
        }
    ]


def _magic_mapper(blueprint_id: int) -> tuple[int, str]:
    return blueprint_id, "cards_prints"


@pytest.mark.asyncio
async def test_webhook_marks_reserved_row_without_breaking_outbox_version(
    test_session_factory,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        inbox = _inbox(user_id, "reserved-event")
        session.add(inbox)
        await session.flush()
        item = _item(
            user_id,
            reserved_quantity=1,
            sync_state="pending",
        )
        session.add(item)
        await session.flush()
        item_id = item.id
        inbox_id = inbox.id

        changed = await _quarantine_inventory(
            session,
            user_id=user_id,
            environment="partial",
            inbox_id=inbox_id,
            product_ids=["101"],
            full_quarantine=False,
        )
        assert changed == 1

    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        assert item.sync_state == "pending"
        assert item.sync_uncertain_event_id == inbox_id
        assert item.row_version == 4


@pytest.mark.asyncio
async def test_preack_inbox_and_quarantine_commit_atomically(
    test_session_factory,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    payload = {
        "id": "preack-atomic",
        "cause": "order.create",
        "mode": "live",
        "data": {
            "id": 77,
            "state": "paid",
            "via_cardtrader_zero": False,
            "order_items": [{"product_id": 101, "quantity": 1}],
        },
    }
    async with test_session_factory() as session, session.begin():
        settings = _settings(user_id)
        inbox = _inbox(user_id, "preack-atomic")
        inbox.status = "received"
        item = _item(user_id)
        session.add_all([settings, inbox, item])
        await session.flush()
        item_id, inbox_id = item.id, inbox.id

        result = await WebhookLedgerProcessor().prepare_inbox(
            session,
            inbox=inbox,
            payload=payload,
            settings=settings,
        )
        assert result["status"] == "reconcile_required"
        assert item.quantity == 5

    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        inbox = await session.get(WebhookInbox, inbox_id)
        assert item.quantity == 5
        assert item.sync_state == "uncertain"
        assert item.sync_uncertain_event_id == inbox_id
        assert inbox.status == "reconcile_pending"


@pytest.mark.asyncio
async def test_reserved_marker_keeps_inbox_pending_until_later_snapshot(
    test_session_factory,
    monkeypatch,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        inbox = _inbox(user_id, "reserved-reconcile")
        session.add(inbox)
        await session.flush()
        item = _item(
            user_id,
            reserved_quantity=1,
            sync_state="pending",
            marker=inbox.id,
        )
        session.add(item)
        await session.flush()
        item_id, inbox_id = item.id, inbox.id

    async def load_snapshot(session, _sync_settings):
        item = await session.get(UserInventoryItem, item_id)
        await session.commit()
        return [item], 0, _export(8), inbox_id

    monkeypatch.setattr(reconciler, "_load_local_and_export", load_snapshot)

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        first = await reconciler.reconcile_user_apply(session, settings, _magic_mapper)
    assert first["applied"]["skipped_unsafe"] == 1

    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        inbox = await session.get(WebhookInbox, inbox_id)
        assert item.quantity == 5
        assert item.sync_uncertain_event_id == inbox_id
        assert inbox.status == "reconcile_pending"
        item.reserved_quantity = 0
        item.sync_state = "synced"
        await session.commit()

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        second = await reconciler.reconcile_user_apply(session, settings, _magic_mapper)
    assert second["status"] == "ok"

    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        inbox = await session.get(WebhookInbox, inbox_id)
        assert item.quantity == 8
        assert item.sync_state == "synced"
        assert item.sync_uncertain_event_id is None
        assert inbox.status == "completed"


@pytest.mark.asyncio
async def test_event_during_export_cannot_be_cleared_by_older_watermark(
    test_session_factory,
    monkeypatch,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        old_inbox = _inbox(user_id, "before-export")
        session.add(old_inbox)
        await session.flush()
        item = _item(
            user_id,
            sync_state="uncertain",
            marker=old_inbox.id,
        )
        session.add(item)
        await session.flush()
        item_id, old_id = item.id, old_inbox.id

    new_id: int | None = None

    async def load_with_concurrent_webhook(session, _sync_settings):
        nonlocal new_id
        item = await session.get(UserInventoryItem, item_id)
        await session.commit()
        new_inbox = _inbox(user_id, "during-export")
        session.add(new_inbox)
        await session.flush()
        new_id = new_inbox.id
        item.sync_state = "uncertain"
        item.sync_uncertain_event_id = new_id
        await session.commit()
        return [item], 0, _export(9), old_id

    monkeypatch.setattr(reconciler, "_load_local_and_export", load_with_concurrent_webhook)

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        result = await reconciler.reconcile_user_apply(session, settings, _magic_mapper)
    assert result["status"] == "superseded"

    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        inboxes = {
            row.webhook_id: row
            for row in (
                await session.execute(select(WebhookInbox).where(WebhookInbox.user_id == user_id))
            )
            .scalars()
            .all()
        }
        assert item.quantity == 5
        assert item.sync_uncertain_event_id == new_id
        assert inboxes["before-export"].status == "reconcile_pending"
        assert inboxes["during-export"].status == "reconcile_pending"


@pytest.mark.asyncio
async def test_outbound_completion_during_export_wins_row_version_cas(
    test_session_factory,
    monkeypatch,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        item = _item(user_id)
        session.add(item)
        await session.flush()
        item_id = item.id

    async def load_then_complete_outbound(session, _sync_settings):
        stale_item = await session.get(UserInventoryItem, item_id)
        await session.commit()
        session.expunge(stale_item)
        async with test_session_factory() as outbound:
            await outbound.execute(
                update(UserInventoryItem)
                .where(UserInventoryItem.id == item_id)
                .values(
                    quantity=6,
                    row_version=5,
                    sync_state="synced",
                    sync_uncertain_event_id=None,
                )
            )
            await outbound.commit()
        return [stale_item], 0, _export(9), 0

    monkeypatch.setattr(
        reconciler,
        "_load_local_and_export",
        load_then_complete_outbound,
    )

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        result = await reconciler.reconcile_user_apply(session, settings, _magic_mapper)

    assert result["applied"]["skipped_unsafe"] == 1
    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        assert item.quantity == 6
        assert item.row_version == 5


@pytest.mark.asyncio
async def test_new_product_is_not_created_from_snapshot_superseded_by_webhook(
    test_session_factory,
    monkeypatch,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))

    async def load_then_receive_webhook(session, _sync_settings):
        await session.commit()
        session.add(_inbox(user_id, "new-product-race"))
        await session.commit()
        return [], 0, _export(3), 0

    monkeypatch.setattr(
        reconciler,
        "_load_local_and_export",
        load_then_receive_webhook,
    )

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        result = await reconciler.reconcile_user_apply(session, settings, _magic_mapper)

    assert result["status"] == "superseded"
    async with test_session_factory() as session:
        count = (
            (
                await session.execute(
                    select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        assert count == []


@pytest.mark.asyncio
async def test_marketplace_listing_is_disabled_until_marker_clears(
    test_session_factory,
) -> None:
    if not hasattr(UserInventoryItem, "game_id"):
        pytest.skip("migration game_id non ancora applicata")

    user_id = uuid.uuid4()
    listing_id = uuid.uuid4()
    async with test_session_factory() as session:
        await session.execute(text("""
                CREATE TEMP TABLE mkt_listings (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL,
                    cardtrader_article_id bigint,
                    quantity integer NOT NULL,
                    status text NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT NOW()
                )
                """))
        item = _item(user_id, sync_state="uncertain", marker=12)
        session.add(item)
        await session.flush()
        await session.execute(
            text("""
                INSERT INTO mkt_listings
                    (id, user_id, cardtrader_article_id, quantity, status)
                VALUES
                    (CAST(:id AS uuid), CAST(:user_id AS uuid), 101, 5, 'active')
                """),
            {"id": str(listing_id), "user_id": str(user_id)},
        )

        assert await project_inventory_to_marketplace(session, user_id, "partial")
        pending = (
            await session.execute(
                text("SELECT quantity, status FROM mkt_listings WHERE id=:id"),
                {"id": listing_id},
            )
        ).one()
        assert pending == (0, "pending_sync")

        item.sync_state = "synced"
        item.sync_uncertain_event_id = None
        item.quantity = 7
        await session.flush()
        await project_inventory_to_marketplace(session, user_id, "partial")
        active = (
            await session.execute(
                text("SELECT quantity, status FROM mkt_listings WHERE id=:id"),
                {"id": listing_id},
            )
        ).one()
        assert active == (7, "active")


@pytest.mark.asyncio
async def test_registered_reconcile_task_reaches_terminal_status(
    test_session_factory,
    monkeypatch,
) -> None:
    user_id = uuid.uuid4()
    task_id = str(uuid.uuid4())
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        await session.flush()
        session.add(
            SyncOperation(
                user_id=user_id,
                operation_id=task_id,
                operation_type="reconcile",
                status="pending",
            )
        )

    @asynccontextmanager
    async def isolated_test_session():
        async with test_session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr(periodic_sync, "get_isolated_db_session", isolated_test_session)

    changed = await periodic_sync._update_registered_reconcile(
        task_id,
        str(user_id),
        "completed",
        {"result": {"status": "ok"}},
    )
    missing = await periodic_sync._update_registered_reconcile(
        str(uuid.uuid4()),
        str(user_id),
        "completed",
    )

    assert changed is True
    assert missing is False
    async with test_session_factory() as session:
        operation = (
            await session.execute(
                select(SyncOperation).where(SyncOperation.operation_id == task_id)
            )
        ).scalar_one()
        assert operation.status == "completed"
        assert operation.completed_at is not None
        assert operation.operation_metadata == {"result": {"status": "ok"}}
