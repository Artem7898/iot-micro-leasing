from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict, field_serializer
from enum import Enum


class EventType(str, Enum):
    SESSION_START = "session_start"
    USAGE_TICK = "usage_tick"
    SESSION_END = "session_end"


class UsagePingRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {
                "device_id": "dev_884x9c",
                "event_type": "usage_tick",
                "timestamp": "2025-11-15T14:32:01Z",
                "payload": {"units_consumed": 2.5}
            }
        ]
    })

    device_id: str = Field(..., min_length=8, max_length=36, pattern=r'^dev_[a-zA-Z0-9]+$')
    event_type: EventType
    timestamp: datetime
    payload: dict = Field(default_factory=dict, max_length=1024)


class BillingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: str
    device_id: str
    status: str
    total_cost_usd: Decimal = Field(..., ge=0, decimal_places=4)
    message: str

    @field_serializer('total_cost_usd', when_used='json')
    def serialize_cost(self, value: Decimal) -> float:
        return float(value)