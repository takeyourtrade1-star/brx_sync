"""
Pytest configuration and shared fixtures for BRX Sync tests.
"""
import asyncio
from typing import AsyncGenerator, Generator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.config import get_settings
from app.core.database import Base

settings = get_settings()


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def test_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Create a test database session.
    
    Uses a separate test database (configure via TEST_DATABASE_URL env var).
    """
    # Use test database URL if available, otherwise use main DB
    test_db_url = getattr(settings, "TEST_DATABASE_URL", None) or settings.DATABASE_URL.replace(
        "/brx_sync", "/brx_sync_test"
    )
    
    engine = create_async_engine(
        test_db_url,
        pool_pre_ping=True,
        echo=False,
    )
    
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    async with async_session() as session:
        yield session
        await session.rollback()
    
    # Cleanup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    from unittest.mock import AsyncMock, MagicMock
    
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
    from unittest.mock import AsyncMock, MagicMock
    
    client_mock = AsyncMock()
    client_mock.get_products_export = AsyncMock(return_value=[])
    client_mock.bulk_update_products = AsyncMock(return_value={"job": "test-job-id"})
    client_mock.get_job_status = AsyncMock(return_value={"state": "completed"})
    client_mock.delete_product = AsyncMock(return_value={"status": "deleted"})
    
    return client_mock
