"""Shared persistence models for devices, billing, and reliable invoice delivery."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, UniqueConstraint, text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class Device(SQLModel, table=True):
    __tablename__ = "device"
    __table_args__ = (
        CheckConstraint(
            "rate_per_unit >= 0 AND rate_per_unit <= 999999.9999", name="ck_device_rate_per_unit"
        ),
    )

    id: str = Field(primary_key=True, max_length=36)
    rate_per_unit: Decimal = Field(default=Decimal("0.0000"), max_digits=10, decimal_places=4)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class UsageSession(SQLModel, table=True):
    __tablename__ = "usagesession"
    __table_args__ = (
        CheckConstraint(
            "total_cost >= 0 AND total_cost <= 99999999.9999", name="ck_usagesession_total_cost"
        ),
        Index(
            "uq_usagesession_active_device",
            "device_id",
            unique=True,
            sqlite_where=text("is_active = true"),
            postgresql_where=text("is_active = true"),
        ),
        Index("ix_usagesession_device_created", "device_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=36)
    device_id: str = Field(foreign_key="device.id", max_length=36)
    total_cost: Decimal = Field(default=Decimal("0.0000"), max_digits=12, decimal_places=4)
    is_active: bool = Field(default=True)
    last_ping_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class UsageEvent(SQLModel, table=True):
    """Durable idempotency receipt; retains the accepted monetary response."""

    __tablename__ = "usageevent"

    idempotency_key: str = Field(primary_key=True, max_length=128)
    request_hash: str = Field(max_length=64)
    device_id: str = Field(foreign_key="device.id", max_length=36, index=True)
    session_id: str = Field(foreign_key="usagesession.id", max_length=36)
    total_cost: Decimal = Field(max_digits=12, decimal_places=4)
    status: str = Field(max_length=16)
    message: str = Field(max_length=128)
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class InvoiceOutbox(SQLModel, table=True):
    """Created in the billing transaction and delivered at least once to Celery."""

    __tablename__ = "invoiceoutbox"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_invoiceoutbox_session"),
        Index(
            "ix_invoiceoutbox_delivery", "completed_at", "published_at", "locked_at", "created_at"
        ),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=36)
    session_id: str = Field(foreign_key="usagesession.id", max_length=36)
    device_id: str = Field(foreign_key="device.id", max_length=36)
    total_cost: Decimal = Field(max_digits=12, decimal_places=4)
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    published_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    completed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    locked_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    attempts: int = Field(default=0)
    last_error: str | None = Field(default=None, max_length=128)
