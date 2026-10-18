from decimal import Decimal

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models.device import Device, UsageSession


class DeviceService:
    def __init__(self, db: Session):
        self.db = db

    def get_balance(self, device_id: str) -> dict:
        device = self.db.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        # The partial unique index guarantees this lookup has at most one row.
        debt = self.db.exec(
            select(UsageSession.total_cost)
            .where(UsageSession.device_id == device_id, UsageSession.is_active.is_(True))
            .limit(1)
        ).first()
        return {
            "device_id": device.id,
            "active_session_debt_usd": debt if debt is not None else Decimal("0.0000"),
            "is_active": device.is_active,
        }
