from decimal import Decimal
from enum import StrEnum

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class EventType(StrEnum):
    SESSION_START = "session_start"
    USAGE_TICK = "usage_tick"
    SESSION_END = "session_end"


class UsagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    units_consumed: Decimal = Field(
        default=Decimal("0"), ge=0, le=1_000_000_000, max_digits=14, decimal_places=4
    )

    @field_validator("units_consumed", mode="before")
    @classmethod
    def numeric_units(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
            raise ValueError("units_consumed must be a finite decimal number")
        return value


class UsagePingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(..., min_length=8, max_length=36, pattern=r"^dev_[a-zA-Z0-9]+$")
    event_type: EventType
    timestamp: AwareDatetime
    payload: UsagePayload = Field(default_factory=UsagePayload)

    @model_validator(mode="after")
    def positive_tick(self) -> UsagePingRequest:
        if self.event_type == EventType.USAGE_TICK and self.payload.units_consumed <= 0:
            raise ValueError("usage_tick requires positive units_consumed")
        return self


class BillingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: str
    device_id: str
    status: str
    total_cost_usd: Decimal = Field(..., ge=0, decimal_places=4)
    message: str

    @field_serializer("total_cost_usd", when_used="json")
    def serialize_cost(self, value: Decimal) -> str:
        return format(value, ".4f")
