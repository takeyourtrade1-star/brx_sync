"""
Redis client for rate limiting and Celery broker.
"""
import logging
import threading
from typing import Optional

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_redis_client: Optional[Redis] = None


async def get_redis() -> Optional[Redis]:
    """Get or create async Redis client."""
    global _redis_client

    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=False,
                health_check_interval=30,
            )
            await _redis_client.ping()
            logger.info("Redis connection established")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            _redis_client = None

    return _redis_client


async def close_redis() -> None:
    """Close Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis connection closed")


# Redis sync connection pool (for rate limiting, circuit breaker, etc.)
_redis_sync_pool = None
_redis_sync_pool_lock = threading.Lock()

def get_redis_sync():
    """
    Get synchronous Redis client for Celery tasks and rate limiting.
    Uses connection pooling to avoid creating new connections for each request.
    """
    import redis
    global _redis_sync_pool
    
    if _redis_sync_pool is None:
        with _redis_sync_pool_lock:
            # Double-check after acquiring lock
            if _redis_sync_pool is None:
                _redis_sync_pool = redis.ConnectionPool.from_url(
                    settings.REDIS_URL,
                    encoding="utf-8",
                    decode_responses=True,
                    max_connections=50,  # Limit pool size to prevent connection exhaustion
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    retry_on_timeout=False,
                    health_check_interval=30,
                )
                logger.info("Redis sync connection pool initialized (max_connections=50)")
    
    return redis.Redis(connection_pool=_redis_sync_pool)
