"""PostgreSQL concurrency regression for API sync admission."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The deployed Sync image includes PyMySQL. The minimal DB-test image does not;
# this test exercises PostgreSQL-only admission and supplies a non-functional
# import shim so app.core.database can be imported without expanding test scope.
if importlib.util.find_spec("pymysql") is None:
    pymysql_stub = types.ModuleType("pymysql")
    pymysql_stub.Connection = object
    pymysql_stub.connect = lambda **_kwargs: None
    pymysql_stub.cursors = types.SimpleNamespace(DictCursor=object)
    sys.modules["pymysql"] = pymysql_stub
    sys.modules["pymysql.cursors"] = pymysql_stub.cursors

from app.api.v1.routes import sync as sync_routes
from app.models.inventory import Base, SyncOperation, SyncStatusEnum, UserSyncSettings


class SyncSingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        database_url = os.getenv("TEST_DATABASE_URL")
        if not database_url:
            self.skipTest("TEST_DATABASE_URL is required")
        self.engine = create_async_engine(database_url, pool_pre_ping=True)
        async with self.engine.begin() as connection:
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

    async def test_two_concurrent_start_requests_publish_one_bulk_task(self) -> None:
        user_id = uuid.uuid4()
        async with self.sessions() as setup_session:
            setup_session.add(
                UserSyncSettings(
                    user_id=user_id,
                    cardtrader_token_encrypted="encrypted-token",
                    sync_status=SyncStatusEnum.IDLE.value,
                    execution_mode="partial",
                    writes_enabled=False,
                )
            )
            await setup_session.commit()

        class _EncryptionManager:
            @staticmethod
            def decrypt(_value: str) -> str:
                return "configured-cardtrader-token"

        published_task_ids: list[str] = []

        def publish(*_args, **kwargs):
            task_id = str(kwargs["task_id"])
            published_task_ids.append(task_id)
            return SimpleNamespace(id=task_id)

        first_holds_lock = asyncio.Event()
        release_first = asyncio.Event()
        original_register = sync_routes._register_task_before_enqueue
        register_calls = 0

        async def gated_register(*args, **kwargs):
            nonlocal register_calls
            register_calls += 1
            if register_calls == 1:
                first_holds_lock.set()
                await release_first.wait()
            return await original_register(*args, **kwargs)

        async def start_once():
            async with self.sessions() as session:
                return await sync_routes.start_sync(
                    str(user_id),
                    force=False,
                    verified_user_id=str(user_id),
                    session=session,
                )

        with (
            patch(
                "app.core.crypto.get_encryption_manager",
                return_value=_EncryptionManager(),
            ),
            patch.object(sync_routes.initial_bulk_sync, "apply_async", side_effect=publish),
            patch.object(
                sync_routes,
                "_register_task_before_enqueue",
                side_effect=gated_register,
            ),
        ):
            first = asyncio.create_task(start_once())
            await asyncio.wait_for(first_holds_lock.wait(), timeout=2)
            second = asyncio.create_task(start_once())
            await asyncio.sleep(0.1)
            second_waited_for_lock = not second.done()
            release_first.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertTrue(
            second_waited_for_lock,
            "the second transaction must wait for the per-user row lock",
        )
        self.assertEqual(first_result.task_id, second_result.task_id)
        self.assertEqual(published_task_ids, [first_result.task_id])

        async with self.sessions() as verify_session:
            operation_count = (
                await verify_session.execute(
                    select(func.count()).select_from(SyncOperation).where(
                        SyncOperation.user_id == user_id,
                        SyncOperation.operation_type == "bulk_sync",
                        SyncOperation.status.in_(("pending", "processing")),
                    )
                )
            ).scalar_one()
        self.assertEqual(operation_count, 1)


if __name__ == "__main__":
    unittest.main()
