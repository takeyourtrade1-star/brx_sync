"""
FastAPI application entry point for BRX Sync Microservice.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.routes import sync as sync_router
from app.core.config import get_settings
from app.core.database import close_mysql_connection
from app.core.exception_handlers import EXCEPTION_HANDLERS
from app.core.logging import get_logger, setup_logging
from app.core.redis_client import close_redis

# Setup logging first
setup_logging()

settings = get_settings()
logger = get_logger(__name__)


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
    lifespan=lifespan,
)

# CORS middleware
# In production, replace "*" with specific allowed origins
allowed_origins = settings.ALLOWED_ORIGINS.split(",") if hasattr(settings, "ALLOWED_ORIGINS") else ["*"]
if "*" in allowed_origins and settings.ENVIRONMENT == "production":
    logger.warning("CORS allow_origins is set to '*' in production! This is a security risk.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


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
    from app.core.health import get_health_status
    
    health_status = await get_health_status()
    
    if health_status["status"] == "healthy":
        return health_status
    else:
        return JSONResponse(
            status_code=503,
            content=health_status,
        )


@app.get("/health")
async def health():
    """
    Detailed health check endpoint.
    
    Returns detailed status for all components.
    """
    from app.core.health import get_health_status
    
    return await get_health_status()


@app.get("/metrics")
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

# Serve static files (frontend test)
try:
    import os
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        
        @app.get("/test")
        async def test_page():
            """Redirect to test page."""
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/static/index.html")
except Exception as e:
    logger.warning(f"Static files not available: {e}")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
