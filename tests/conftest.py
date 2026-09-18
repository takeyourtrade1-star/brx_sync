"""
Pytest configuration and shared fixtures for BRX Sync tests.
"""
import asyncio
import os
from typing import AsyncGenerator, Generator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.inventory import Base
# Register catalog queue tables before Base.metadata.create_all runs. Tests
# that do not exercise the queue still use the same disposable schema fixture.
from app.models import catalog as _catalog_models  # noqa: F401


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def test_db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create a clean schema only on the explicitly configured disposable DB."""
    # Mai ripiegare sul DB applicativo: questi test creano e distruggono tabelle.
    test_db_url = os.getenv("TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL non configurato")
    
    engine = create_async_engine(
        test_db_url,
        pool_pre_ping=True,
        echo=False,
    )
    
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    # Cleanup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest.fixture(scope="function")
def test_session_factory(test_db_engine: AsyncEngine):
    return async_sessionmaker(
        test_db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture(scope="function")
async def test_db_session(test_session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    from unittest.mock import AsyncMock
    
    redis_mock = AsyncMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.set = AsyncMock(return_value=True)
    redis_mock.delete = AsyncMock(return_value=1)
    redis_mock.incr = AsyncMock(return_value=1)
    redis_mock.hgetall = AsyncMock(return_value={})
    redis_mock.hset = AsyncMock(return_value=1)
    
    return redis_mock


@pytest.fixture
def mock_cardtrader_client():
    """Mock CardTrader client."""
    from unittest.mock import AsyncMock
    
    client_mock = AsyncMock()
    client_mock.get_products_export = AsyncMock(return_value=[])
    client_mock.bulk_update_products = AsyncMock(return_value={"job": "test-job-id"})
    client_mock.get_job_status = AsyncMock(return_value={"state": "completed"})
    client_mock.delete_product = AsyncMock(return_value={"status": "deleted"})
    
    return client_mock
