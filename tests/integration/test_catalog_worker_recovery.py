"""Worker recovery across the MySQL commit and Search outbox boundary."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.catalog import (
    CatalogImportJob,
    CatalogImportRequest,
    CatalogImportStatus,
    CatalogIndexOutbox,
    CatalogOutboxStatus,
)
from app.models.inventory import SyncStatusEnum, UserInventoryItem, UserSyncSettings
from app.services.catalog_importer import CatalogImportResult
from app.services.catalog_import_queue import enqueue_catalog_import
from app.tasks import catalog_tasks

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _isolated_sessions(session_factory):
    """Give the worker the same commit/rollback boundary as production."""

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


def _profile(user_id: uuid.UUID) -> UserSyncSettings:
    return UserSyncSettings(
        user_id=user_id,
        cardtrader_token_encrypted="encrypted",
        sync_status=SyncStatusEnum.ACTIVE.value,
        execution_mode="partial",
        mode_version=1,
        writes_enabled=False,
    )


def _product(stock_id: str, blueprint_id: int = 393523) -> dict[str, object]:
    return {
        "id": stock_id,
        "game_id": 1,
        "category_id": 1,
        "blueprint_id": blueprint_id,
        "quantity": 46,
        "price_cents": 250,
        "environment": "partial",
        "expansion": {"id": 4415, "name": "Marvel Super Heroes", "code": "msh"},
    }


class _FakeCatalogImporter:
    """Stand-in for a committed MySQL writer plus exact CT/Scryfall reads."""

    def __init__(self) -> None:
        self.calls = 0
        self.mysql_committed = False

    async def import_blueprint(self, blueprint_id: int, product: dict[str, object]) -> CatalogImportResult:
        self.calls += 1
        # The real importer returns only after its MySQL transaction commits.
        self.mysql_committed = True
        return CatalogImportResult(
            blueprint_id=blueprint_id,
            scryfall_id="4d8c8ceb-84cd-46d2-9230-ab6ca4569334",
            local_print_id=99543,
            document={
                "id": "mtg_99543",
                "cardtrader_id": blueprint_id,
                "oracle_id": "11111111-1111-4111-8111-111111111111",
                "name": "Marvel Super Heroes",
                "search_tokens": ["mar", "marvel"],
            },
        )


class _FlakyPublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, document: dict[str, object]) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Search is temporarily unavailable")


@pytest.mark.asyncio
async def test_mysql_commit_survives_search_failure_and_retry_maps_without_stock_mutation(
    test_session_factory,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "CATALOG_IMPORT_ENABLED", True)
    monkeypatch.setattr(settings, "CATALOG_SEARCH_PUBLISH_ENABLED", True)
    monkeypatch.setattr(
        catalog_tasks,
        "get_isolated_db_session",
        _isolated_sessions(test_session_factory),
    )
    monkeypatch.setattr(catalog_tasks, "_invalidate_blueprint_mapping_cache", AsyncMock())

    user_id = uuid.uuid4()
    stock_id = "recovery-stock"
    original_quantity = 46
    original_row_version = 12
    product = _product(stock_id)
    async with test_session_factory() as session:
        async with session.begin():
            session.add(_profile(user_id))
            session.add(
                UserInventoryItem(
                    user_id=user_id,
                    blueprint_id=393523,
                    game_id=1,
                    quantity=original_quantity,
                    reserved_quantity=0,
                    price_cents=250,
                    properties={"condition": "Near Mint"},
                    external_stock_id=stock_id,
                    source="cardtrader",
                    environment="partial",
                    lifecycle_status="active",
                    sync_state="synced",
                    sync_uncertain_event_id=None,
                    row_version=original_row_version,
                    mapping_status="missing",
                )
            )
            await enqueue_catalog_import(session, user_id, product)

    importer = _FakeCatalogImporter()
    publisher = _FlakyPublisher()
    catalog_tasks.register_catalog_importer_factory(lambda _user_id, _product: importer)
    catalog_tasks.register_catalog_index_publisher(publisher)
    try:
        async with test_session_factory() as session:
            queued_job = (await session.execute(select(CatalogImportJob))).scalar_one()
            queued_job_id = int(queued_job.id)

        import_result = await catalog_tasks._process_catalog_import_job(queued_job_id)
        assert import_result["status"] == "succeeded"
        assert importer.mysql_committed is True
        assert importer.calls == 1

        async with test_session_factory() as session:
            job = (await session.execute(select(CatalogImportJob))).scalar_one()
            outbox = (await session.execute(select(CatalogIndexOutbox))).scalar_one()
            item = (
                await session.execute(
                    select(UserInventoryItem).where(
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.external_stock_id == stock_id,
                    )
                )
            ).scalar_one()
            assert job.status == CatalogImportStatus.SUCCEEDED.value
            assert outbox.status == CatalogOutboxStatus.PENDING.value
            assert item.mapping_status == "missing"
            assert item.quantity == original_quantity
            assert item.row_version == original_row_version
            job_id = job.id
            outbox_id = outbox.id

        first_index = await catalog_tasks._process_index_outbox(outbox_id)
        assert first_index["status"] == "failed"

        async with test_session_factory() as session, session.begin():
            outbox = await session.get(CatalogIndexOutbox, outbox_id)
            assert outbox is not None
            outbox.next_attempt_at = datetime.now(timezone.utc)

        async with test_session_factory() as session:
            outbox = await session.get(CatalogIndexOutbox, outbox_id)
            item = (
                await session.execute(
                    select(UserInventoryItem).where(
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.external_stock_id == stock_id,
                    )
                )
            ).scalar_one()
            assert outbox is not None and outbox.status == CatalogOutboxStatus.FAILED.value
            assert item.mapping_status == "missing"
            assert item.quantity == original_quantity
            assert item.row_version == original_row_version

        second_index = await catalog_tasks._process_index_outbox(outbox_id)
        assert second_index["status"] == "succeeded"
        assert publisher.calls == 2

        async with test_session_factory() as session:
            job = await session.get(CatalogImportJob, job_id)
            outbox = await session.get(CatalogIndexOutbox, outbox_id)
            item = (
                await session.execute(
                    select(UserInventoryItem).where(
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.external_stock_id == stock_id,
                    )
                )
            ).scalar_one()
            request = (await session.execute(select(CatalogImportRequest))).scalar_one()

        assert job is not None and job.status == CatalogImportStatus.SUCCEEDED.value
        assert outbox is not None and outbox.status == CatalogOutboxStatus.SUCCEEDED.value
        assert request.mode_version == 1
        assert item.mapping_status == "mapped"
        assert item.quantity == original_quantity
        assert item.row_version == original_row_version
    finally:
        catalog_tasks.register_catalog_importer_factory(None)
        catalog_tasks.register_catalog_index_publisher(None)
