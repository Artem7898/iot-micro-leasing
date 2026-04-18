from decimal import Decimal
from pydantic import BaseModel, field_serializer


class DeviceBalanceResponse(BaseModel):
    device_id: str
    active_session_debt_usd: Decimal
    is_active: bool

    @field_serializer('active_session_debt_usd', when_used='json')
    def serialize_cost(self, value: Decimal) -> float:
        return float(value)