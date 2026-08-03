"""
Database connection management for PostgreSQL (async) and MySQL (sync).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import pymysql
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# PostgreSQL async engine
pg_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=10,
    connect_args=settings.postgres_connect_args,
    echo=settings.DEBUG,
)

AsyncSessionLocal = async_sessionmaker(
    pg_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get async PostgreSQL database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Get async PostgreSQL database session as context manager."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def transaction_with_timeout(
    session: AsyncSession,
    timeout_seconds: Optional[int] = None,
) -> AsyncGenerator[None, None]:
    """
    Context manager for database transactions with timeout.
    
    Wraps session.begin() with a timeout to prevent long-running transactions.
    Note: The timeout applies to the entire transaction block, not individual operations.
    
    Args:
        session: Database session
        timeout_seconds: Timeout in seconds (defaults to DB_TRANSACTION_TIMEOUT)
        
    Raises:
        asyncio.TimeoutError: If transaction exceeds timeout
    """
    timeout = timeout_seconds or settings.DB_TRANSACTION_TIMEOUT
    try:
        async with asyncio.timeout(timeout):
            async with session.begin():
                yield
    except TimeoutError as exc:
        await session.rollback()
        logger.error("Database transaction exceeded its configured timeout")
        raise asyncio.TimeoutError(
            f"Database transaction exceeded timeout of {timeout} seconds"
        ) from exc


async def execute_with_deadlock_retry(
    operation,
    max_retries: int = 3,
    base_delay: float = 0.1,
) -> any:
    """
    Execute a database operation with automatic retry on deadlock.
    
    PostgreSQL deadlock error code: 40001 (serialization_failure) or 40P01 (deadlock_detected)
    
    Args:
        operation: Async callable to execute
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff
        
    Returns:
        Result of the operation
        
    Raises:
        Exception: If operation fails after all retries
    """
    import random
    from sqlalchemy.exc import OperationalError
    
    for attempt in range(max_retries):
        try:
            return await operation()
        except OperationalError as e:
            # Check if it's a deadlock error
            error_code = getattr(e.orig, 'pgcode', None)
            is_deadlock = (
                error_code == '40001' or  # serialization_failure
                error_code == '40P01' or  # deadlock_detected
                'deadlock' in str(e).lower() or
                'serialization' in str(e).lower()
            )
            
            if is_deadlock and attempt < max_retries - 1:
                # Exponential backoff with jitter
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
                logger.warning(
                    f"Deadlock detected (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying after {delay:.2f}s..."
                )
                await asyncio.sleep(delay)
                continue
            else:
                # Not a deadlock or max retries reached
                raise
    else:
        # This should never be reached, but just in case
        raise Exception(f"Operation failed after {max_retries} attempts")


# MySQL connection pool (sync, read-only for blueprint mapping)
# Using pymysql connection pool for thread-safe connection management
from pymysql import cursors
from queue import Empty, Full, Queue
import threading

_mysql_pool: Optional[Queue] = None
_mysql_pool_lock = threading.Lock()
_mysql_open_connections = 0
def _get_mysql_pool_size() -> int:
    """Get MySQL pool size from settings."""
    return getattr(settings, 'MYSQL_POOL_SIZE', 5)

def _get_mysql_pool_max_overflow() -> int:
    """Get MySQL pool max overflow from settings."""
    return getattr(settings, 'MYSQL_POOL_MAX_OVERFLOW', 5)


def _create_mysql_connection() -> pymysql.Connection:
    """Create a new MySQL connection."""
    options = {
        "host": settings.MYSQL_HOST,
        "port": settings.MYSQL_PORT,
        "user": settings.MYSQL_USER,
        "password": settings.MYSQL_PASSWORD.get_secret_value(),
        "database": settings.MYSQL_DATABASE,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 5,
        "read_timeout": 10,
        "write_timeout": 10,
        "local_infile": False,
        "autocommit": True,  # Read-only operations
    }
    if settings.mysql_ssl_context is not None:
        options["ssl"] = settings.mysql_ssl_context
    return pymysql.connect(**options)


def _init_mysql_pool() -> None:
    """Initialize MySQL connection pool."""
    global _mysql_pool, _mysql_open_connections
    if _mysql_pool is None:
        pool_size = _get_mysql_pool_size()
        max_overflow = _get_mysql_pool_max_overflow()
        _mysql_pool = Queue(maxsize=pool_size + max_overflow)
        
        # Pre-populate pool with connections
        for _ in range(pool_size):
            try:
                conn = _create_mysql_connection()
                _mysql_pool.put(conn)
                _mysql_open_connections += 1
            except Exception as exc:
                logger.error(
                    "Failed to create MySQL connection for pool (%s)",
                    type(exc).__name__,
                )
                # Continue with fewer connections if some fail
        
        logger.info(f"MySQL connection pool initialized with {_mysql_pool.qsize()} connections")


def get_mysql_connection() -> pymysql.Connection:
    """
    Get MySQL connection from pool (thread-safe).
    
    Returns:
        MySQL connection from pool
        
    Raises:
        Exception: If connection cannot be obtained
    """
    global _mysql_pool, _mysql_open_connections
    
    with _mysql_pool_lock:
        if _mysql_pool is None:
            _init_mysql_pool()
    
    assert _mysql_pool is not None

    # Prefer an idle connection. If none is available, reserve a bounded
    # overflow slot before opening a new socket. Once capacity is reached,
    # wait for a borrower to return a connection instead of bypassing the pool.
    try:
        conn = _mysql_pool.get_nowait()
    except Empty:
        create_new = False
        with _mysql_pool_lock:
            capacity = _get_mysql_pool_size() + _get_mysql_pool_max_overflow()
            if _mysql_open_connections < capacity:
                _mysql_open_connections += 1
                create_new = True
        if create_new:
            try:
                conn = _create_mysql_connection()
            except Exception:
                with _mysql_pool_lock:
                    _mysql_open_connections -= 1
                raise
        else:
            try:
                conn = _mysql_pool.get(timeout=5)
            except Empty as exc:
                raise TimeoutError("MySQL connection pool exhausted") from exc

    try:
        conn.ping(reconnect=False)
    except Exception:
        logger.warning("MySQL pooled connection was stale; replacing its bounded slot")
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn = _create_mysql_connection()
        except Exception:
            with _mysql_pool_lock:
                _mysql_open_connections -= 1
            raise
    return conn


def return_mysql_connection(conn: pymysql.Connection) -> None:
    """
    Return MySQL connection to pool.
    
    Args:
        conn: MySQL connection to return
    """
    global _mysql_pool, _mysql_open_connections
    
    if _mysql_pool is None:
        # Pool not initialized, just close the connection
        try:
            conn.close()
        except Exception:
            pass
        return
    
    # Check if connection is still alive
    try:
        conn.ping(reconnect=False)
        
        # Try to put connection back in pool
        try:
            _mysql_pool.put_nowait(conn)
        except Full:
            # Pool is full, close the connection
            try:
                conn.close()
            except Exception:
                pass
            with _mysql_pool_lock:
                _mysql_open_connections -= 1
    except Exception:
        # Connection is dead, close it
        try:
            conn.close()
        except Exception:
            pass
        with _mysql_pool_lock:
            _mysql_open_connections -= 1


def close_mysql_connection() -> None:
    """Close all MySQL connections in pool."""
    global _mysql_pool, _mysql_open_connections
    
    if _mysql_pool is None:
        return
    
    with _mysql_pool_lock:
        while not _mysql_pool.empty():
            try:
                conn = _mysql_pool.get_nowait()
                conn.close()
            except Exception:
                pass
        
        _mysql_pool = None
        _mysql_open_connections = 0
        logger.info("MySQL connection pool closed")


# Context manager for MySQL connections
from contextlib import contextmanager

@contextmanager
def get_mysql_connection_context():
    """
    Context manager for MySQL connections (automatically returns to pool).
    
    Usage:
        with get_mysql_connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ...")
            ...
    """
    conn = None
    try:
        conn = get_mysql_connection()
        yield conn
    finally:
        if conn:
            return_mysql_connection(conn)


# Synchronous PostgreSQL engine (for Celery tasks error handling)
_sync_pg_engine: Optional[Engine] = None


def get_sync_db_engine():
    """Get synchronous PostgreSQL engine for use in Celery tasks (error handling)."""
    from sqlalchemy import create_engine
    
    global _sync_pg_engine
    if _sync_pg_engine is None:
        # Convert async URL to sync URL (use psycopg2)
        sync_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        _sync_pg_engine = create_engine(
            sync_url,
            pool_size=settings.DB_SYNC_POOL_SIZE,
            max_overflow=settings.DB_SYNC_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_timeout=10,
            connect_args=settings.postgres_sync_connect_args,
            echo=settings.DEBUG,
        )
        logger.info("Synchronous PostgreSQL engine created (psycopg2)")
    return _sync_pg_engine


def create_isolated_async_engine():
    """
    Create a new isolated async engine for use in Celery tasks.
    This ensures the engine is bound to the current event loop.
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    
    # Create a new engine bound to the current event loop
    isolated_engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_ISOLATED_POOL_SIZE,
        max_overflow=settings.DB_ISOLATED_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_timeout=10,
        connect_args=settings.postgres_connect_args,
        echo=settings.DEBUG,
    )
    
    return isolated_engine


@asynccontextmanager
async def get_isolated_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Get an isolated async database session for use in Celery tasks.
    Creates a new engine bound to the current event loop.
    This prevents "Task attached to different loop" errors.
    """
    import asyncio
    
    # Get the current running loop (or create one if needed)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, get the event loop (may be None)
        current_loop = asyncio.get_event_loop()
        if current_loop is None or current_loop.is_closed():
            # Create a new loop if none exists or it's closed
            current_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(current_loop)
    
    logger.debug(f"Creating isolated engine for event loop: {id(current_loop)}")
    
    engine = create_isolated_async_engine()
    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    
    session = None
    try:
        session = async_session()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    finally:
        # Close session first - CRITICAL: must be done before disposing engine
        if session:
            try:
                # Close all connections in the session
                await session.close()
                # Small delay to ensure connections are fully closed
                import asyncio
                await asyncio.sleep(0.05)
            except Exception as exc:
                logger.warning("Error closing isolated session (%s)", type(exc).__name__)
        
        # Dispose the engine after use to free resources
        # CRITICAL: This must be done before the event loop is closed
        try:
            # Dispose all connections in the pool
            await engine.dispose(close=True)
            # Small delay to ensure disposal is complete
            import asyncio
            await asyncio.sleep(0.05)
            logger.debug(f"Disposed isolated engine for event loop: {id(current_loop)}")
        except Exception as exc:
            logger.warning("Error disposing isolated engine (%s)", type(exc).__name__)
