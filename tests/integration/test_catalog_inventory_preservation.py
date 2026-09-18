"""PostgreSQL coverage for pending CardTrader catalog rows and transactions."""

from contextlib import asynccontextmanager
import uuid

import pytest
from sqlalchemy import select

from app.api.v1.routes.sync import get_inventory
from app.core.config import get_settings
from app.models.catalog import CatalogImportJob, CatalogImportRequest, CatalogIndexOutbox
from app.models.inventory import SyncStatusEnum, UserInventoryItem, UserSyncSettings
from app.services import reconciler

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
    monkeypatch.setattr(get_settings(), "CATALOG_IMPORT_ENABLED", True)


def _settings(user_id: uuid.UUID) -> UserSyncSettings:
    return UserSyncSettings(
        user_id=user_id,
        cardtrader_token_encrypted="encrypted",
        sync_status=SyncStatusEnum.ACTIVE.value,
        execution_mode="partial",
        writes_enabled=False,
    )


def _pending_product(*, product_id: int, blueprint_id: int, quantity: int) -> dict:
    return {
        "id": product_id,
        "game_id": 1,
        "blueprint_id": blueprint_id,
        "category_id": 1,
        "quantity": quantity,
        "price_cents": 250,
        "name_en": "Pending Card",
        "image_url": "https://cdn.cardtrader.com/pending.jpg",
        "expansion": {"id": 4415, "name": "Example Set", "code": "EX"},
        "properties_hash": {"condition": "Near Mint"},
    }


def _loader(products):
    async def load(_session, _settings):
        return [], 0, products, 0

    return load


@pytest.mark.asyncio
async def test_unmapped_snapshot_preserves_stock_queues_one_global_job_and_is_visible(
    test_session_factory,
    monkeypatch,
):
    user_id = uuid.uuid4()
    product = _pending_product(product_id=7001, blueprint_id=393523, quantity=46)
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))

    monkeypatch.setattr(reconciler, "_load_local_and_export", _loader([product]))

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        result = await reconciler.reconcile_user_apply(
            session,
            settings,
            lambda _blueprint_id: None,
        )

    assert result["status"] == "ok"
    assert result["raw_rows"] == 1
    assert result["raw_copies"] == 46
    assert result["imported_rows"] == 0
    assert result["unmapped_rows"] == 1
    assert result["unmapped_copies"] == 46
    assert result["incomplete"] is True

    async with test_session_factory() as session:
        item = (
            await session.execute(
                select(UserInventoryItem).where(
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.external_stock_id == "7001",
                )
            )
        ).scalar_one()
        job = (
            await session.execute(
                select(CatalogImportJob).where(
                    CatalogImportJob.provider == "cardtrader",
                    CatalogImportJob.game_id == 1,
                    CatalogImportJob.blueprint_id == 393523,
                )
            )
        ).scalar_one()
        request = (
            await session.execute(
                select(CatalogImportRequest).where(
                    CatalogImportRequest.user_id == user_id,
                    CatalogImportRequest.external_stock_id == "7001",
                )
            )
        ).scalar_one()

        assert (item.game_id, item.quantity, item.mapping_status) == (1, 46, "missing")
        assert item.properties[reconciler.CATALOG_METADATA_KEY]["expansion"]["id"] == 4415
        assert job.status == "pending"
        assert request.job_id == job.id
        assert request.environment == "partial"

        response = await get_inventory(
            str(user_id),
            limit=100,
            offset=0,
            include_history=False,
            include_anomalies=False,
            verified_user_id=str(user_id),
            session=session,
        )

    assert response.total == 1
    assert len(response.items) == 1
    assert response.items[0].game_id == 1
    assert response.items[0].quantity == 46
    assert response.items[0].mapping_status == "missing"
    assert response.items[0].catalog_metadata["expansion"]["id"] == 4415
    assert response.raw_rows == 1
    assert response.raw_copies == 46
    assert response.unmapped_rows == 1
    assert response.unmapped_copies == 46
    assert response.incomplete is True


@pytest.mark.asyncio
async def test_unmapped_stock_and_catalog_job_roll_back_together(test_session_factory, monkeypatch):
    user_id = uuid.uuid4()
    product = _pending_product(product_id=7002, blueprint_id=393524, quantity=2)
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))

    async def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("test snapshot failure")

    monkeypatch.setattr(reconciler, "_load_local_and_export", _loader([product]))
    monkeypatch.setattr(reconciler, "_record_snapshot", fail_snapshot)

    with pytest.raises(RuntimeError, match="test snapshot failure"):
        async with test_session_factory() as session:
            settings = await session.get(UserSyncSettings, user_id)
            await reconciler.reconcile_user_apply(
                session,
                settings,
                lambda _blueprint_id: None,
            )

    async with test_session_factory() as session:
        item = (
            await session.execute(
                select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
            )
        ).scalar_one_or_none()
        job = (
            await session.execute(
                select(CatalogImportJob).where(CatalogImportJob.blueprint_id == 393524)
            )
        ).scalar_one_or_none()
        request = (
            await session.execute(
                select(CatalogImportRequest).where(CatalogImportRequest.user_id == user_id)
            )
        ).scalar_one_or_none()

    assert item is None
    assert job is None
    assert request is None


@pytest.mark.asyncio
async def test_mysql_mapping_waits_for_search_ack_but_old_mapping_without_job_is_allowed(
    test_session_factory,
    monkeypatch,
):
    """A MySQL print must not become mapped during the Search outbox window."""

    user_id = uuid.uuid4()
    blueprint_id = 393525
    product = _pending_product(product_id=7003, blueprint_id=blueprint_id, quantity=3)
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        job = CatalogImportJob(
            provider="cardtrader",
            game_id=1,
            blueprint_id=blueprint_id,
            status="succeeded",
            source_json={"blueprint_id": blueprint_id},
        )
        session.add(job)
        await session.flush()
        session.add(
            CatalogIndexOutbox(
                job_id=job.id,
                document_id=f"cardtrader:{blueprint_id}",
                document_json={"id": f"cardtrader:{blueprint_id}"},
                status="pending",
            )
        )

    monkeypatch.setattr(reconciler, "_load_local_and_export", _loader([product]))
    mapper = lambda _blueprint_id: (64100, "cards_prints")

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        first = await reconciler.reconcile_user_apply(session, settings, mapper)
    assert first["unmapped_rows"] == 1
    async with test_session_factory() as session:
        item = (
            await session.execute(
                select(UserInventoryItem).where(
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.external_stock_id == "7003",
                )
            )
        ).scalar_one()
        assert item.mapping_status == "missing"
        outbox = (
            await session.execute(
                select(CatalogIndexOutbox).where(CatalogIndexOutbox.job_id == job.id)
            )
        ).scalar_one()
        outbox.status = "succeeded"
        await session.commit()

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        second = await reconciler.reconcile_user_apply(session, settings, mapper)
    assert second["imported_rows"] == 1
    async with test_session_factory() as session:
        item = (
            await session.execute(
                select(UserInventoryItem).where(
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.external_stock_id == "7003",
                )
            )
        ).scalar_one()
        assert item.mapping_status == "mapped"

    # Existing deployments can have valid MySQL mappings from before the
    # repair queue existed.  The gate must not quarantine those blueprints.
    legacy_user_id = uuid.uuid4()
    legacy_product = _pending_product(product_id=7004, blueprint_id=393526, quantity=1)
    async with test_session_factory() as session, session.begin():
        session.add(_settings(legacy_user_id))
    monkeypatch.setattr(reconciler, "_load_local_and_export", _loader([legacy_product]))
    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, legacy_user_id)
        legacy_result = await reconciler.reconcile_user_apply(session, settings, mapper)
    assert legacy_result["imported_rows"] == 1
    async with test_session_factory() as session:
        item = (
            await session.execute(
                select(UserInventoryItem).where(
                    UserInventoryItem.user_id == legacy_user_id,
                    UserInventoryItem.external_stock_id == "7004",
                )
            )
        ).scalar_one()
        assert item.mapping_status == "mapped"


@pytest.mark.asyncio
async def test_legacy_null_magic_row_is_zeroed_after_two_absent_exports(test_session_factory, monkeypatch):
    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_settings(user_id))
        item = UserInventoryItem(
            user_id=user_id,
            blueprint_id=501,
            game_id=None,
            quantity=7,
            reserved_quantity=0,
            price_cents=250,
            properties={},
            external_stock_id="legacy-501",
            source="cardtrader",
            environment="partial",
            lifecycle_status="active",
            sync_state="synced",
            sync_uncertain_event_id=None,
            row_version=1,
            mapping_status="unsupported",
        )
        session.add(item)

    async def load_legacy(session, _settings):
        rows = (
            await session.execute(
                select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
            )
        ).scalars().all()
        return list(rows), 0, [], 0

    monkeypatch.setattr(reconciler, "_load_local_and_export", load_legacy)
    mapper = lambda blueprint_id: (64048, "cards_prints") if blueprint_id == 501 else None

    async with test_session_factory() as session:
        settings = await session.get(UserSyncSettings, user_id)
        first = await reconciler.reconcile_user_apply(session, settings, mapper)
    assert first["status"] == "ok"

    async with test_session_factory() as session:
        stale = (
            await session.execute(
                select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
            )
        ).scalar_one()
        assert stale.game_id is None
        assert stale.missing_snapshot_count == 1
        settings = await session.get(UserSyncSettings, user_id)
        second = await reconciler.reconcile_user_apply(session, settings, mapper)
    assert second["status"] == "ok"

    async with test_session_factory() as session:
        repaired = (
            await session.execute(
                select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
            )
        ).scalar_one()

    assert repaired.game_id == 1
    assert repaired.mapping_status == "mapped"
    assert repaired.quantity == 0
    assert repaired.lifecycle_status == "sold_out"
    assert repaired.sync_state == "synced"
    assert repaired.missing_snapshot_count == 2
