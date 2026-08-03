"""PostgreSQL regressions for the canonical sync-status enum."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text

from app.api.v1.routes.sync import (
    _mark_enqueue_failed,
    _register_task_before_enqueue,
)
from app.models.inventory import SyncStatusEnum, UserSyncSettings
from app.tasks import sync_tasks

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.mark.asyncio
async def test_sync_status_paths_round_trip_through_physical_pg_enum(
    test_session_factory,
    monkeypatch,
) -> None:
    user_id = uuid.uuid4()
    operation_id = str(uuid.uuid4())

    async with test_session_factory() as session:
        settings = UserSyncSettings(
            user_id=user_id,
            cardtrader_token_encrypted="encrypted",
            sync_status=SyncStatusEnum.IDLE.value,
            execution_mode="partial",
            writes_enabled=False,
        )
        session.add(settings)
        await session.commit()

        await _register_task_before_enqueue(
            session,
            user_id,
            operation_id,
            "bulk_sync",
        )
        await session.refresh(settings)
        assert settings.sync_status == SyncStatusEnum.INITIAL_SYNC.value

        await _mark_enqueue_failed(
            session,
            operation_id,
            RuntimeError("simulated enqueue failure"),
        )
        await session.refresh(settings)
        assert settings.sync_status == SyncStatusEnum.ERROR.value

    @asynccontextmanager
    async def isolated_test_session():
        async with test_session_factory() as session:
            yield session

    monkeypatch.setattr(
        sync_tasks,
        "get_isolated_db_session",
        isolated_test_session,
    )

    for expected_status in (
        SyncStatusEnum.INITIAL_SYNC.value,
        SyncStatusEnum.ACTIVE.value,
        SyncStatusEnum.ERROR.value,
    ):
        await sync_tasks._update_sync_status(
            user_id,
            expected_status,
            error="expected" if expected_status == SyncStatusEnum.ERROR.value else None,
        )
        async with test_session_factory() as session:
            settings = await session.get(UserSyncSettings, user_id)
            assert settings is not None
            assert settings.sync_status == expected_status

    async with test_session_factory() as session:
        physical_type = (
            await session.execute(
                text("""
                    SELECT pg_typeof(sync_status)::text
                    FROM user_sync_settings
                    WHERE user_id = CAST(:user_id AS uuid)
                    """),
                {"user_id": str(user_id)},
            )
        ).scalar_one()
        physical_labels = (await session.execute(text("""
                    SELECT enum_label.enumlabel
                    FROM pg_enum AS enum_label
                    JOIN pg_type AS enum_type
                      ON enum_type.oid = enum_label.enumtypid
                    WHERE enum_type.typname = 'sync_status_enum'
                    ORDER BY enum_label.enumsortorder
                    """))).scalars().all()

    assert physical_type == "sync_status_enum"
    assert physical_labels == [
        SyncStatusEnum.IDLE.value,
        SyncStatusEnum.INITIAL_SYNC.value,
        SyncStatusEnum.ACTIVE.value,
        SyncStatusEnum.ERROR.value,
    ]
