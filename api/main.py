"""FastAPI application: authenticated billing and explicit operational readiness."""

from contextlib import asynccontextmanager

import redis
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select
from starlette.exceptions import HTTPException

from api.core.config import settings
from api.core.logging import configure_logging
from api.core.middleware import RequestBoundaryMiddleware
from api.core.rate_limit import limiter
from api.core.security import require_api_key
from api.dependencies.db import create_db_and_tables, get_engine
from api.models.device import Device, InvoiceOutbox, UsageEvent, UsageSession
from api.v1.endpoints.device import router as device_router
from api.v1.endpoints.usage import router as usage_router

configure_logging(settings.LOG_LEVEL)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.AUTO_CREATE_SCHEMA:
        create_db_and_tables()
    yield
    get_engine().dispose()


app = FastAPI(
    title="IoT Micro-Leasing API",
    version="2.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestBoundaryMiddleware, max_bytes=settings.MAX_REQUEST_BYTES)


@app.exception_handler(RequestValidationError)
def validation_error(request: Request, exc: RequestValidationError):
    # Pydantic inputs/context can contain credentials, huge payloads, or exceptions.
    errors = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
    )


@app.exception_handler(RateLimitExceeded)
def rate_limit_error(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429, content={"detail": "Rate limit exceeded"}, headers={"Retry-After": "60"}
    )


def rate_limit_storage_error(request: Request, exc: Exception):
    logger.warning("rate_limit_storage_unavailable", error_type=type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "Rate limiting temporarily unavailable"},
        headers={"Retry-After": "5"},
    )


# SlowAPI dispatches middleware failures by exact exception type rather than MRO.
for storage_exception in (
    redis.RedisError,
    redis.ConnectionError,
    redis.TimeoutError,
    redis.AuthenticationError,
    redis.exceptions.BusyLoadingError,
    redis.exceptions.MaxConnectionsError,
):
    app.add_exception_handler(storage_exception, rate_limit_storage_error)


@app.exception_handler(SQLAlchemyError)
def database_error(request: Request, exc: SQLAlchemyError):
    logger.exception(
        "database_request_failed",
        error_type=type(exc).__name__,
        request_id=request.state.request_id,
    )
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


@app.exception_handler(Exception)
def unexpected_error(request: Request, exc: Exception):
    logger.exception(
        "request_failed", error_type=type(exc).__name__, request_id=request.state.request_id
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"},
    )


protected = [Depends(require_api_key)]
app.include_router(usage_router, prefix="/api/v1/usage", tags=["usage"], dependencies=protected)
app.include_router(device_router, prefix="/api/v1", dependencies=protected)


@app.get("/health", tags=["operations"])
@limiter.exempt
def health_check():
    """Process liveness. Dependency readiness is reported separately by /ready."""
    return {"status": "ok"}


@app.get("/ready", tags=["operations"])
@limiter.exempt
def readiness_check():
    checks = {"database": False, "redis": False}
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            for column in (
                Device.created_at,
                UsageSession.created_at,
                UsageEvent.request_hash,
                InvoiceOutbox.completed_at,
            ):
                connection.execute(select(column).limit(0))
        checks["database"] = True
    except SQLAlchemyError as exc:
        logger.warning("readiness_database_unavailable", error_type=type(exc).__name__)
    try:
        with redis.Redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        ) as client:
            checks["redis"] = bool(client.ping())
    except redis.RedisError as exc:
        logger.warning("readiness_redis_unavailable", error_type=type(exc).__name__)
    ready = all(checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "unavailable", "checks": checks},
        status_code=200 if ready else 503,
    )
