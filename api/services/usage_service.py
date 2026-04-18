import structlog
from sqlmodel import Session, select
from fastapi import HTTPException
from datetime import datetime, timezone
from decimal import Decimal
from api.tasks.generate_invoice import generate_invoice_task


from api.schemas.usage import UsagePingRequest, BillingResponse, EventType
from api.models.device import Device, UsageSession

logger = structlog.get_logger(__name__)


class UsageService:
    def __init__(self, db: Session):
        self.db = db

    async def process_ping(self, request: UsagePingRequest) -> BillingResponse:
        log = logger.bind(device_id=request.device_id, event=request.event_type)
        log.info("processing_usage_ping")

        device = self.db.exec(
            select(Device).where(Device.id == request.device_id)
        ).first()

        if not device or not device.is_active:
            log.warning("device_not_found_or_inactive")
            raise HTTPException(status_code=404, detail="Device not found or deactivated.")


        now = datetime.now(timezone.utc)
        time_diff_seconds = (now - request.timestamp).total_seconds()

        if time_diff_seconds < 0:
            # Защита от запросов из будущего (глюки IoT часов, подмена данных)
            log.warning("future_request_detected", diff=time_diff_seconds)
            raise HTTPException(status_code=400, detail="Request timestamp is in the future.")

        if time_diff_seconds > 300:
            # Защита от старых запросов (плохой Wi-Fi, replay-атаки)
            log.warning("stale_request_detected", diff=time_diff_seconds)
            raise HTTPException(status_code=400, detail="Request timestamp is too old (max 5m lag).")

        session = self._get_or_create_session(device, request)
        units = request.payload.get("units_consumed", 0.0)

        cost = round(Decimal(str(units)) * device.rate_per_unit, 4)
        session.total_cost += cost
        session.last_ping_at = datetime.now(timezone.utc)

        if request.event_type == EventType.SESSION_END:
            session.is_active = False
            log.info("session_closed", session_id=session.id, total_cost=session.total_cost)

            from api.tasks.generate_invoice import generate_invoice_task
            generate_invoice_task.delay(
                session_id=session.id,
                device_id=device.id,
                total_cost=str(session.total_cost)
            )

        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)

        return BillingResponse(
            session_id=session.id,
            device_id=device.id,
            status="active" if session.is_active else "closed",
            total_cost_usd=session.total_cost,
            message="Ping accepted."
        )

    def _get_or_create_session(self, device: Device, req: UsagePingRequest) -> UsageSession:
        if req.event_type == EventType.SESSION_START:
            existing = self.db.exec(
                select(UsageSession).where(
                    UsageSession.device_id == device.id,
                    UsageSession.is_active == True
                )
            ).first()
            if existing:
                existing.is_active = False
                self.db.add(existing)

            new_session = UsageSession(device_id=device.id)
            self.db.add(new_session)
            self.db.flush()
            return new_session

        active_session = self.db.exec(
            select(UsageSession).where(
                UsageSession.device_id == device.id,
                UsageSession.is_active == True
            )
        ).first()

        if not active_session:
            raise HTTPException(status_code=409, detail="No active session found for this device.")

        return active_session