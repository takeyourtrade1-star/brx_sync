"""
FastAPI application entry point for BRX Sync Microservice.
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.routes import sync as sync_router
from app.api import internal_routes
from app.api.internal_dependencies import require_internal_scope
from app.core.config import get_settings
from app.core.database import close_mysql_connection
from app.core.exception_handlers import EXCEPTION_HANDLERS
from app.core.logging import get_logger, setup_logging
from app.core.http_security import RequestBodyLimitMiddleware, SecurityHeadersMiddleware
from app.core.probe_cache import AsyncProbeCache
from app.core.redis_client import close_redis

# Setup logging first
setup_logging()

settings = get_settings()
logger = get_logger(__name__)
secure_environment = settings.ENVIRONMENT in {"staging", "production"}


async def _critical_dependencies_ready() -> bool:
    from app.core.health import get_health_status

    health_status = await get_health_status()
    return health_status.get("status") == "healthy"


readiness_probe = AsyncProbeCache(
    _critical_dependencies_ready,
    ttl_seconds=4.0,
    timeout_seconds=3.0,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    # Startup
    logger.info("Starting BRX Sync Microservice")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Debug: {settings.DEBUG}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down BRX Sync Microservice")
    await close_redis()
    close_mysql_connection()


# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.APP_VERSION,
    description="Microservice for synchronizing inventory between Ebartex and CardTrader V2 API",
    debug=settings.DEBUG,
    docs_url=None if secure_environment else "/docs",
    redoc_url=None if secure_environment else "/redoc",
    openapi_url=(
        None if secure_environment else "/openapi.json"
    ),
    lifespan=lifespan,
)

# CORS middleware
# Parse ALLOWED_ORIGINS (comma-separated, no spaces around URLs)
_raw = (settings.ALLOWED_ORIGINS or "").strip()
allowed_origins = [o.strip() for o in _raw.split(",") if o.strip()] if _raw else ["*"]
if "*" in allowed_origins and settings.ENVIRONMENT not in {
    "development",
    "test",
}:
    raise RuntimeError("ALLOWED_ORIGINS='*' is allowed only in local/test environments")
logger.info("CORS allowed_origins: %s", allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials="*" not in allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[host.strip() for host in settings.TRUSTED_HOSTS.split(",")],
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=settings.REQUEST_MAX_BODY_BYTES,
    max_messages=settings.REQUEST_MAX_BODY_MESSAGES,
)
app.add_middleware(SecurityHeadersMiddleware, hsts=secure_environment)


# Register exception handlers
for exception_type, handler in EXCEPTION_HANDLERS.items():
    app.add_exception_handler(exception_type, handler)


# Health check endpoints
@app.get("/health/live")
async def health_live():
    """Liveness probe."""
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    """
    Readiness probe.
    
    Checks all critical dependencies (PostgreSQL, Redis, MySQL, Celery).
    Returns 200 if all are healthy, 503 otherwise.
    """
    if await readiness_probe.get():
        return {"status": "ready"}
    else:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable"},
        )


@app.get("/health")
async def health():
    """Minimal public health response; dependency details stay in server logs."""
    return {"status": "healthy"}


@app.get(
    "/metrics",
    dependencies=[Depends(require_internal_scope("metrics:read"))],
    include_in_schema=False,
)
async def metrics():
    """
    Prometheus metrics endpoint.
    
    Returns metrics in Prometheus text format.
    """
    from app.core.prometheus_metrics import get_metrics_response
    from fastapi.responses import Response
    
    metrics_text, content_type = get_metrics_response()
    return Response(content=metrics_text, media_type=content_type)


# Include routers
app.include_router(sync_router.router, prefix=settings.API_V1_STR)
app.include_router(internal_routes.router)

# Serve static files (frontend test)
try:
    import os
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    if settings.test_endpoints_enabled and os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        
        @app.get("/test")
        async def test_page():
            """Redirect to test page."""
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/static/index.html")
except Exception:
    logger.warning("Static test files are unavailable")


@app.get("/")
async def root():
    """Root endpoint."""
    payload = {
        "service": settings.APP_NAME,
        "status": "running",
    }
    if settings.ENVIRONMENT in {"development", "test"}:
        payload["version"] = settings.APP_VERSION
    return payload


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        proxy_headers=False,
        limit_concurrency=128,
        limit_max_requests=10_000,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=30,
    )
