"""API authentication is fail closed; development bypass requires explicit configuration."""

import secrets
from typing import Annotated

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from api.core.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Annotated[str | None, Security(api_key_header)] = None) -> None:
    if not settings.API_AUTH_ENABLED and settings.ENVIRONMENT in {"development", "test"}:
        return
    if settings.API_KEY is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "API authentication is not configured"
        )
    if (
        key is None
        or len(key) > 512
        or not secrets.compare_digest(key.encode(), settings.API_KEY.get_secret_value().encode())
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
