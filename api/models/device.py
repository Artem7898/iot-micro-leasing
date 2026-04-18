from sqlmodel import SQLModel, Field
from decimal import Decimal
import uuid
from datetime import datetime


class Device(SQLModel, table=True):
    __tablename__ = "device"

    id: str = Field(primary_key=True, max_length=36)
    rate_per_unit: Decimal = Field(default=Decimal("0.0"), max_digits=10, decimal_places=4)
    is_active: bool = Field(default=True)


class UsageSession(SQLModel, table=True):
    __tablename__ = "usagesession"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    device_id: str = Field(foreign_key="device.id", max_length=36)
    total_cost: Decimal = Field(default=Decimal("0.0"), max_digits=12, decimal_places=4)
    is_active: bool = Field(default=True)
    last_ping_at: datetime | None = Field(default=None)