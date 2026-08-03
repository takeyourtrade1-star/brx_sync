"""
Health check utilities for BRX Sync microservice.

Provides health checks for all dependencies (PostgreSQL, Redis, MySQL, Celery)
and aggregated health status.
"""
import asyncio
from typing import Any, Dict, Optional

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_db_session_context, get_mysql_connection
from app.core.logging import get_logger
from app.core.redis_client import get_redis

settings = get_settings()
logger = get_logger(__name__)


async def check_postgresql() -> Dict[str, Any]:
    """
    Check PostgreSQL connection and query performance.
    
    Returns:
        Dict with status and details
    """
    try:
        async with get_db_session_context() as session:
            # Simple query to check connection
            result = await session.execute(text("SELECT 1"))
            result.scalar()
            
            # Check connection pool
            # Note: This is a simplified check - in production, you might want
            # to check pool size, active connections, etc.
            
            return {
                "status": "healthy",
                "message": "PostgreSQL connection successful",
            }
    except Exception as exc:
        logger.error("PostgreSQL health check failed (%s)", type(exc).__name__)
        return {
            "status": "unhealthy",
            "message": "PostgreSQL connection failed",
            "error_type": type(exc).__name__,
        }


async def check_redis() -> Dict[str, Any]:
    """
    Check Redis connection.
    
    Returns:
        Dict with status and details
    """
    try:
        redis = await get_redis()
        if not redis:
            return {
                "status": "unhealthy",
                "message": "Redis client not available",
            }
        
        # Ping Redis
        await redis.ping()
        
        return {
            "status": "healthy",
            "message": "Redis connection successful",
        }
    except Exception as exc:
        logger.error("Redis health check failed (%s)", type(exc).__name__)
        return {
            "status": "unhealthy",
            "message": "Redis connection failed",
            "error_type": type(exc).__name__,
        }


def check_mysql() -> Dict[str, Any]:
    """
    Check MySQL connection pool (synchronous).
    
    Returns:
        Dict with status and details
    """
    try:
        from app.core.database import get_mysql_connection_context
        
        with get_mysql_connection_context() as conn:
            # Simple query to check connection
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            
            return {
                "status": "healthy",
                "message": "MySQL connection pool healthy",
            }
    except Exception as exc:
        logger.error("MySQL health check failed (%s)", type(exc).__name__)
        return {
            "status": "unhealthy",
            "message": "MySQL connection pool failed",
            "error_type": type(exc).__name__,
        }


async def check_celery() -> Dict[str, Any]:
    """
    Check Celery broker connectivity.
    
    Returns:
        Dict with status and details
    """
    try:
        from app.tasks.celery_app import celery_app
        
        # Verify broker connectivity without enabling Celery's remote-control
        # command plane on production workers.
        def check_broker_connection():
            with celery_app.connection_for_read() as connection:
                connection.ensure_connection(max_retries=0, timeout=1)

        await asyncio.wait_for(
            asyncio.to_thread(check_broker_connection),
            timeout=2.0,
        )
        return {
            "status": "healthy",
            "message": "Celery broker connection successful",
        }
    except Exception as exc:
        logger.error("Celery health check failed (%s)", type(exc).__name__)
        return {
            "status": "unhealthy",
            "message": "Celery broker check failed",
            "error_type": type(exc).__name__,
        }


async def get_health_status() -> Dict[str, Any]:
    """
    Get aggregated health status for all components.
    
    Returns:
        Dict with overall status and component statuses
    """
    # Run all health checks in parallel
    postgresql_status, redis_status, celery_status = await asyncio.gather(
        check_postgresql(),
        check_redis(),
        check_celery(),
        return_exceptions=True,
    )
    
    # The MySQL driver is synchronous; never block the FastAPI event loop.
    mysql_status = await asyncio.to_thread(check_mysql)
    
    # Handle exceptions
    if isinstance(postgresql_status, Exception):
        postgresql_status = {
            "status": "unhealthy",
            "message": "PostgreSQL check raised an exception",
        }
    
    if isinstance(redis_status, Exception):
        redis_status = {
            "status": "unhealthy",
            "message": "Redis check raised an exception",
        }
    
    if isinstance(celery_status, Exception):
        celery_status = {
            "status": "unhealthy",
            "message": "Celery check raised an exception",
        }
    
    # Determine overall status
    component_statuses = [
        postgresql_status.get("status"),
        redis_status.get("status"),
        mysql_status.get("status"),
        celery_status.get("status"),
    ]
    
    if all(status == "healthy" for status in component_statuses):
        overall_status = "healthy"
    elif any(status == "unhealthy" for status in component_statuses):
        overall_status = "unhealthy"
    else:
        overall_status = "degraded"
    
    return {
        "status": overall_status,
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "components": {
            "postgresql": postgresql_status,
            "redis": redis_status,
            "mysql": mysql_status,
            "celery": celery_status,
        },
    }
