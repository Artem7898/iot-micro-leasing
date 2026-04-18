import structlog
from sqlmodel import Session, select
from fastapi import HTTPException
from decimal import Decimal

from api.models.device import Device, UsageSession

logger = structlog.get_logger(__name__)

class DeviceService:
    def __init__(self, db: Session):
        self.db = db

    def get_balance(self, device_id: str) -> dict:
        device = self.db.exec(select(Device).where(Device.id == device_id)).first()
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Ищем активную сессию
        active_session = self.db.exec(
            select(UsageSession).where(
                UsageSession.device_id == device_id,
                UsageSession.is_active == True
            )
        ).first()

        debt = active_session.total_cost if active_session else Decimal("0.0")

        return {
            "device_id": device.id,
            "active_session_debt_usd": debt,
            "is_active": device.is_active
        }