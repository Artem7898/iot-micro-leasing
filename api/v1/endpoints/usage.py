from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import Session

from api.dependencies.db import get_db_session
from api.schemas.usage import BillingResponse, UsagePingRequest
from api.services.usage_service import UsageService

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/ping", response_model=BillingResponse)
def process_usage_ping(
    request: Request,
    body: UsagePingRequest,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    ],
    db: Annotated[Session, Depends(get_db_session)],
) -> BillingResponse:
    """Accept a device event once; reuse its key when retrying a request."""
    try:
        return UsageService(db).process_ping(body, idempotency_key)
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        logger.exception("unexpected_error_processing_ping")
        raise HTTPException(status_code=500, detail="Internal server error") from error
