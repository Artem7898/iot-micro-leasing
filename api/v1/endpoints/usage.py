from fastapi import APIRouter, Depends, Request, HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address
from structlog import get_logger
from sqlmodel import Session

from api.dependencies.db import get_db_session
from api.schemas.usage import UsagePingRequest, BillingResponse
from api.services.usage_service import UsageService

logger = get_logger(__name__)
router = APIRouter()

# Инициализация Rate Limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["500/minute"])

@router.post("/ping")


@limiter.limit("100/second")
async def process_usage_ping(
    request: Request,
    body: UsagePingRequest,
    db: Session = Depends(get_db_session)
) -> BillingResponse:
    """
    Принимает эвенты от устройств.
    Подсчитывает стоимость по факту потребления (Pay-per-use).
    """
    try:
        service = UsageService(db)
        return await service.process_ping(body)
    except HTTPException:
        raise  # FastAPI сам обработает HTTPException
    except Exception as e:
        logger.exception("unexpected_error_processing_ping")
        raise HTTPException(status_code=500, detail="Internal server error")