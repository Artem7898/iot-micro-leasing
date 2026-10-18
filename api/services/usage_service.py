"""Serialized per-device billing with durable request deduplication."""

import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from hashlib import sha256

import structlog
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from api.models.device import Device, InvoiceOutbox, UsageEvent, UsageSession
from api.schemas.usage import BillingResponse, EventType, UsagePingRequest
from api.services.invoice_service import dispatch_pending_invoices

logger = structlog.get_logger(__name__)
MONEY_QUANTUM = Decimal("0.0001")
MAX_COST = Decimal("99999999.9999")


def request_fingerprint(request: UsagePingRequest) -> str:
    canonical = request.model_dump(mode="json")
    canonical["timestamp"] = request.timestamp.astimezone(UTC).isoformat()
    canonical["payload"]["units_consumed"] = str(request.payload.units_consumed.normalize())
    return sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone=True columns; stored values are UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _receipt_response(receipt: UsageEvent) -> BillingResponse:
    return BillingResponse(
        session_id=receipt.session_id,
        device_id=receipt.device_id,
        status=receipt.status,
        total_cost_usd=receipt.total_cost,
        message=receipt.message,
    )


class UsageService:
    def __init__(self, db: Session):
        self.db = db

    def process_ping(self, request: UsagePingRequest, idempotency_key: str) -> BillingResponse:
        fingerprint = request_fingerprint(request)
        outbox_id: str | None = None
        try:
            # UPDATE obtains a write lock on SQLite and a row lock on PostgreSQL.
            # Lock before reading balances to prevent lost updates and double starts.
            self.db.execute(
                update(Device)
                .where(Device.id == request.device_id)
                .values(id=Device.id)
                .execution_options(synchronize_session=False)
            )
            receipt = self.db.get(UsageEvent, idempotency_key)
            if receipt is not None:
                response = self._replay(receipt, fingerprint)
                self.db.rollback()
                return response
            device = self.db.exec(
                select(Device)
                .where(Device.id == request.device_id)
                .execution_options(populate_existing=True)
            ).first()
            if device is None:
                raise HTTPException(status_code=404, detail="Device not found.")
            if not device.is_active:
                raise HTTPException(status_code=403, detail="Device is deactivated.")
            age = (datetime.now(UTC) - request.timestamp).total_seconds()
            if age < 0:
                raise HTTPException(status_code=400, detail="Request timestamp is in the future.")
            if age > 300:
                raise HTTPException(
                    status_code=400, detail="Request timestamp is too old (max 5m lag)."
                )
            session = self.db.exec(
                select(UsageSession)
                .where(
                    UsageSession.device_id == device.id,
                    UsageSession.is_active.is_(True),
                )
                .limit(1)
                .execution_options(populate_existing=True)
            ).first()
            if request.event_type == EventType.SESSION_START:
                if session is not None:
                    raise HTTPException(
                        status_code=409, detail="A session is already active for this device."
                    )
                session = UsageSession(device_id=device.id)
                self.db.add(session)
                self.db.flush()
            elif session is None:
                raise HTTPException(
                    status_code=409, detail="No active session found for this device."
                )
            if session.last_ping_at is not None and request.timestamp < _as_utc(
                session.last_ping_at
            ):
                raise HTTPException(
                    status_code=409, detail="Request predates the last accepted session event."
                )
            with localcontext() as context:
                context.prec = 40
                cost = (request.payload.units_consumed * device.rate_per_unit).quantize(
                    MONEY_QUANTUM,
                    rounding=ROUND_HALF_EVEN,
                )
                new_total = session.total_cost + cost
            if new_total > MAX_COST:
                raise HTTPException(status_code=409, detail="Session billing limit exceeded.")
            session.total_cost = new_total
            session.last_ping_at = request.timestamp.astimezone(UTC)
            message = "Ping accepted."
            if request.event_type == EventType.SESSION_END:
                session.is_active = False
                outbox = InvoiceOutbox(
                    session_id=session.id, device_id=device.id, total_cost=new_total
                )
                self.db.add(outbox)
                outbox_id = outbox.id
                message = "Ping accepted. Invoice pending delivery."
            receipt = UsageEvent(
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
                device_id=device.id,
                session_id=session.id,
                total_cost=new_total,
                status="active" if session.is_active else "closed",
                message=message,
            )
            self.db.add(receipt)
            self.db.commit()
            response = _receipt_response(receipt)
        except IntegrityError as error:
            self.db.rollback()
            receipt = self.db.get(UsageEvent, idempotency_key)
            if receipt is not None:
                try:
                    return self._replay(receipt, fingerprint)
                finally:
                    self.db.rollback()
            logger.warning("billing_integrity_conflict", device_id=request.device_id)
            raise HTTPException(
                status_code=409,
                detail="Conflicting billing update; retry the same idempotency key.",
            ) from error
        except HTTPException:
            self.db.rollback()
            raise
        except SQLAlchemyError as error:
            self.db.rollback()
            logger.exception("billing_database_failure", device_id=request.device_id)
            raise HTTPException(
                status_code=503,
                detail="Billing database unavailable; retry the same idempotency key.",
            ) from error
        except BaseException:
            self.db.rollback()
            raise
        if outbox_id is not None:
            # The financial transaction is already durable. Broker errors cannot undo it.
            dispatch_pending_invoices(self.db, limit=1, outbox_id=outbox_id)
        logger.info(
            "usage_accepted",
            device_id=request.device_id,
            session_id=response.session_id,
            status=response.status,
        )
        return response

    @staticmethod
    def _replay(receipt: UsageEvent, fingerprint: str) -> BillingResponse:
        if receipt.request_hash != fingerprint:
            raise HTTPException(
                status_code=409, detail="Idempotency key was already used for a different request."
            )
        return _receipt_response(receipt)
