"""PostgreSQL invariants for the durable global catalog queue."""

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.catalog import (
    CatalogImportJob,
    CatalogImportRequest,
    CatalogImportStatus,
    CatalogIndexOutbox,
    CatalogOutboxStatus,
)
from app.models.inventory import SyncStatusEnum, UserInventoryItem, UserSyncSettings
from app.services.catalog_import_queue import (
    claim_catalog_import_job,
    claim_catalog_index_outbox,
    complete_catalog_import_job,
    complete_catalog_index_outbox,
    enqueue_catalog_import,
    enqueue_catalog_index_outbox,
    finalize_catalog_mapping,
    list_due_catalog_import_job_ids,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _profile(
    user_id: uuid.UUID,
    *,
    environment: str = "partial",
    status: str = SyncStatusEnum.ACTIVE.value,
    mode_version: int = 1,
) -> UserSyncSettings:
    return UserSyncSettings(
        user_id=user_id,
        cardtrader_token_encrypted="encrypted",
        sync_status=status,
        execution_mode=environment,
        mode_version=mode_version,
        writes_enabled=False,
    )


def _product(stock_id: int | str, blueprint_id: int = 393523, *, environment: str = "partial") -> dict:
    return {
        "id": stock_id,
        "game_id": 1,
        "category_id": 1,
        "blueprint_id": blueprint_id,
        "quantity": 7,
        "price_cents": 250,
        "environment": environment,
        "expansion": {"id": 4415, "name": "Marvel Super Heroes", "code": "msh"},
    }


@pytest.fixture(autouse=True)
def catalog_queue_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "CATALOG_IMPORT_ENABLED", True)
    monkeypatch.setattr(settings, "CATALOG_IMPORT_MAX_ATTEMPTS", 3)


@pytest.mark.asyncio
async def test_same_blueprint_is_one_global_job_with_one_request_per_observation(
    test_session_factory,
):
    users = [uuid.uuid4() for _ in range(100)]
    async with test_session_factory() as session, session.begin():
        session.add_all([_profile(user_id) for user_id in users])

    async def enqueue_one(user_id: uuid.UUID, stock_id: int) -> None:
        async with test_session_factory() as session:
            await enqueue_catalog_import(session, user_id, _product(stock_id))
            await session.commit()

    await asyncio.gather(
        *(enqueue_one(user_id, index + 1) for index, user_id in enumerate(users))
    )

    async with test_session_factory() as session:
        jobs = (await session.execute(select(func.count()).select_from(CatalogImportJob))).scalar_one()
        requests = (
            await session.execute(select(func.count()).select_from(CatalogImportRequest))
        ).scalar_one()
        request_mode_versions = (
            await session.execute(select(CatalogImportRequest.mode_version))
        ).scalars().all()

    assert jobs == 1
    assert requests == 100
    assert request_mode_versions == [1] * 100


@pytest.mark.asyncio
async def test_queue_writes_roll_back_with_the_callers_transaction(test_session_factory):
    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(_profile(user_id))

    with pytest.raises(RuntimeError, match="rollback sentinel"):
        async with test_session_factory() as session:
            async with session.begin():
                await enqueue_catalog_import(session, user_id, _product("rollback-stock"))
                raise RuntimeError("rollback sentinel")

    async with test_session_factory() as session:
        jobs = (await session.execute(select(func.count()).select_from(CatalogImportJob))).scalar_one()
        requests = (
            await session.execute(select(func.count()).select_from(CatalogImportRequest))
        ).scalar_one()

    assert jobs == 0
    assert requests == 0


@pytest.mark.asyncio
async def test_search_tokens_survive_safe_outbox_projection_without_sensitive_keys(
    test_session_factory,
):
    async with test_session_factory() as session, session.begin():
        job = CatalogImportJob(
            provider="cardtrader",
            game_id=1,
            blueprint_id=393523,
            source_json={"blueprint_id": 393523},
        )
        session.add(job)
        await session.flush()
        await enqueue_catalog_index_outbox(
            session,
            job_id=job.id,
            document_id="mtg_99543",
            document_json={
                "id": "mtg_99543",
                "search_tokens": ["mar", "marvel", 12, {"secret": "drop"}],
                "api_token": "drop",
            },
        )

    async with test_session_factory() as session:
        outbox = (await session.execute(select(CatalogIndexOutbox))).scalar_one()

    assert outbox.document_json["search_tokens"] == ["mar", "marvel"]
    assert "api_token" not in outbox.document_json


@pytest.mark.asyncio
async def test_expired_lease_at_retry_ceiling_becomes_review_and_old_owner_cannot_complete(
    test_session_factory,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "CATALOG_IMPORT_MAX_ATTEMPTS", 1)
    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()
    old_job_token = uuid.uuid4()
    old_outbox_token = uuid.uuid4()

    async with test_session_factory() as session, session.begin():
        session.add(_profile(user_id))
        job = CatalogImportJob(
            provider="cardtrader",
            game_id=1,
            blueprint_id=393523,
            status=CatalogImportStatus.RUNNING.value,
            attempts=1,
            next_attempt_at=now - timedelta(minutes=1),
            lease_token=old_job_token,
            lease_until=now - timedelta(seconds=1),
            source_json={"blueprint_id": 393523},
        )
        session.add(job)
        await session.flush()
        session.add(
            CatalogImportRequest(
                job_id=job.id,
                user_id=user_id,
                game_id=1,
                blueprint_id=393523,
                external_stock_id="lease-stock",
                environment="partial",
                mode_version=1,
                product_json=_product("lease-stock"),
            )
        )
        outbox = CatalogIndexOutbox(
            job_id=job.id,
            document_id="mtg_lease",
            document_json={"id": "mtg_lease"},
            status=CatalogOutboxStatus.RUNNING.value,
            attempts=1,
            next_attempt_at=now - timedelta(minutes=1),
            lease_token=old_outbox_token,
            lease_until=now - timedelta(seconds=1),
        )
        session.add(outbox)
        await session.flush()
        job_id = int(job.id)
        outbox_id = outbox.id

    async with test_session_factory() as session:
        assert await claim_catalog_import_job(session, job_id=job_id, now=now) is None
        await session.commit()

    async with test_session_factory() as session:
        assert (
            await complete_catalog_import_job(
                session,
                job_id,
                old_job_token,
                result_json={"stale": True},
            )
            is False
        )
        await session.commit()

    async with test_session_factory() as session:
        assert await claim_catalog_index_outbox(session, outbox_id=outbox_id, now=now) is None
        await session.commit()

    async with test_session_factory() as session:
        assert (
            await complete_catalog_index_outbox(session, outbox_id, old_outbox_token)
            is False
        )
        job_row = await session.get(CatalogImportJob, job_id)
        outbox_row = await session.get(CatalogIndexOutbox, outbox_id)

    assert job_row is not None and job_row.status == CatalogImportStatus.NEEDS_REVIEW.value
    assert job_row.lease_token is None
    assert outbox_row is not None and outbox_row.status == CatalogOutboxStatus.NEEDS_REVIEW.value
    assert outbox_row.lease_token is None


@pytest.mark.asyncio
async def test_due_dispatch_skips_old_disconnected_profile_and_finds_active_job(
    test_session_factory,
):
    disconnected_user = uuid.uuid4()
    active_user = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(
            _profile(
                disconnected_user,
                status=SyncStatusEnum.INITIAL_SYNC.value,
                environment="partial",
                mode_version=1,
            )
        )
        session.add(_profile(active_user))
        disconnected_job = CatalogImportJob(
            provider="cardtrader",
            game_id=1,
            blueprint_id=393523,
            source_json={"blueprint_id": 393523},
            next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        active_job = CatalogImportJob(
            provider="cardtrader",
            game_id=1,
            blueprint_id=393524,
            source_json={"blueprint_id": 393524},
        )
        session.add_all((disconnected_job, active_job))
        await session.flush()
        session.add_all(
            (
                CatalogImportRequest(
                    job_id=disconnected_job.id,
                    user_id=disconnected_user,
                    game_id=1,
                    blueprint_id=393523,
                    external_stock_id="disconnected-stock",
                    environment="partial",
                    mode_version=1,
                    product_json=_product("disconnected-stock"),
                ),
                CatalogImportRequest(
                    job_id=active_job.id,
                    user_id=active_user,
                    game_id=1,
                    blueprint_id=393524,
                    external_stock_id="active-stock",
                    environment="partial",
                    mode_version=1,
                    product_json=_product("active-stock", blueprint_id=393524),
                ),
            )
        )
        active_job_id = int(active_job.id)

    async with test_session_factory() as session:
        due_ids = await list_due_catalog_import_job_ids(session, limit=1)

    assert due_ids == [active_job_id]


@pytest.mark.asyncio
async def test_initial_sync_can_record_request_but_worker_claim_waits_for_active_profile(
    test_session_factory,
):
    user_id = uuid.uuid4()
    async with test_session_factory() as session, session.begin():
        session.add(
            _profile(
                user_id,
                status=SyncStatusEnum.INITIAL_SYNC.value,
                environment="partial",
                mode_version=7,
            )
        )

    async with test_session_factory() as session:
        await enqueue_catalog_import(session, user_id, _product("initial-stock"))
        await session.commit()

    async with test_session_factory() as session:
        request = (await session.execute(select(CatalogImportRequest))).scalar_one()
        job = await session.get(CatalogImportJob, request.job_id)
        assert request.mode_version == 7
        assert job is not None
        assert await claim_catalog_import_job(session, job_id=job.id) is None
        await session.commit()

    async with test_session_factory() as session, session.begin():
        settings = await session.get(UserSyncSettings, user_id)
        assert settings is not None
        settings.sync_status = SyncStatusEnum.ACTIVE.value

    async with test_session_factory() as session:
        job = (await session.execute(select(CatalogImportJob))).scalar_one()
        lease = await claim_catalog_import_job(session, job_id=job.id)
        assert lease is not None
        await session.commit()


@pytest.mark.asyncio
async def test_request_tracks_profile_switch_and_finalizer_requires_current_version(
    test_session_factory,
):
    user_id = uuid.uuid4()
    product = _product("switch-stock")
    async with test_session_factory() as session, session.begin():
        session.add(_profile(user_id, environment="partial", mode_version=1))

    async with test_session_factory() as session:
        await enqueue_catalog_import(session, user_id, product)
        await session.commit()

    async with test_session_factory() as session, session.begin():
        settings = await session.get(UserSyncSettings, user_id)
        assert settings is not None
        settings.mode_version = 2
        item = UserInventoryItem(
            user_id=user_id,
            blueprint_id=393523,
            game_id=1,
            quantity=31,
            reserved_quantity=0,
            price_cents=250,
            properties={},
            external_stock_id="switch-stock",
            source="cardtrader",
            environment="partial",
            lifecycle_status="active",
            sync_state="synced",
            sync_uncertain_event_id=None,
            row_version=9,
            mapping_status="missing",
        )
        session.add(item)

    async with test_session_factory() as session:
        job = (await session.execute(select(CatalogImportJob))).scalar_one()
        assert await finalize_catalog_mapping(session, job_id=job.id) == 0
        await session.commit()

    async with test_session_factory() as session, session.begin():
        settings = await session.get(UserSyncSettings, user_id)
        assert settings is not None
        settings.execution_mode = "real"
        settings.mode_version = 3

    async with test_session_factory() as session:
        await enqueue_catalog_import(session, user_id, {**product, "environment": "real"})
        await session.commit()

    async with test_session_factory() as session, session.begin():
        settings = await session.get(UserSyncSettings, user_id)
        assert settings is not None
        settings.execution_mode = "partial"
        settings.mode_version = 4

    async with test_session_factory() as session:
        await enqueue_catalog_import(session, user_id, product)
        await session.commit()

    async with test_session_factory() as session:
        requests = (
            await session.execute(
                select(CatalogImportRequest).order_by(CatalogImportRequest.environment)
            )
        ).scalars().all()
        assert [(request.environment, request.mode_version) for request in requests] == [
            ("partial", 4),
            ("real", 3),
        ]
        assert all("mode_version" not in request.product_json for request in requests)
        job = (await session.execute(select(CatalogImportJob))).scalar_one()
        mapped = await finalize_catalog_mapping(session, job_id=job.id)
        item = (
            await session.execute(
                select(UserInventoryItem).where(UserInventoryItem.user_id == user_id)
            )
        ).scalar_one()

    assert mapped == 1
    assert item.mapping_status == "mapped"
    assert item.quantity == 31
    assert item.row_version == 9
