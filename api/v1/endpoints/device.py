from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from api.dependencies.db import get_db_session
from api.schemas.device import DeviceBalanceResponse
from api.services.device_service import DeviceService

router = APIRouter()


@router.get(
    "/devices/{device_id}/balance",
    response_model=DeviceBalanceResponse,
    summary="Get Device Balance"
)
async def get_device_balance(
        device_id: str,
        db: Session = Depends(get_db_session)
) -> DeviceBalanceResponse:
    """
    Возвращает текущий долг устройства в рамках активной сессии.
    """
    service = DeviceService(db)
    balance_data = service.get_balance(device_id)

    # Если устройство заблокировано менеджером
    if not balance_data["is_active"]:
        raise HTTPException(status_code=403, detail="Device is blocked by admin")

    return DeviceBalanceResponse(**balance_data)