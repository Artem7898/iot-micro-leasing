from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import structlog

from api.core.config import settings
from api.v1.endpoints.usage import router as usage_router
from api.v1.endpoints.device import router as device_router

logger = structlog.get_logger()

def custom_validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

def custom_http_exception_handler(request: Request, exc: Exception):
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "Internal Server Error")
    return JSONResponse(status_code=status_code, content={"detail": detail})

def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["500/minute"],
    storage_uri=settings.REDIS_URL
)

app = FastAPI(
    title="IoT Micro-Leasing API",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json"
)

app.state.limiter = limiter
app.add_exception_handler(RequestValidationError, custom_validation_exception_handler)
app.add_exception_handler(Exception, custom_http_exception_handler)
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

app.include_router(usage_router, prefix="/api/v1/usage", tags=["usage"])
app.include_router(device_router, prefix="/api/v1")

@app.get("/health")
async def health_check():
    return {"status": "ok", "redis_connected": True}