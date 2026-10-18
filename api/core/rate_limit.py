"""One Redis-backed limiter shared by every API route and worker process."""

from slowapi import Limiter
from slowapi.util import get_remote_address

from api.core.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMIT],
    storage_uri=settings.REDIS_URL,
    storage_options={"socket_connect_timeout": 2, "socket_timeout": 2, "max_connections": 16},
    enabled=settings.RATE_LIMIT_ENABLED,
    swallow_errors=False,
    in_memory_fallback_enabled=False,
)
