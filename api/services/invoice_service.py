"""Bounded, lease-based delivery of committed invoice outbox entries."""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import or_, update
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from api.models.device import InvoiceOutbox

logger = structlog.get_logger(__name__)


def dispatch_pending_invoices(
    db: Session, *, limit: int = 100, outbox_id: str | None = None
) -> int:
    """Publish at most 100 entries; expired leases recover interrupted dispatchers.

    Delivery is at least once: a process can stop after publishing but before the
    database acknowledgement. The invoice task must tolerate repeated execution.
    """
    from api.tasks.generate_invoice import generate_invoice_task

    if not 1 <= limit <= 100:
        raise ValueError("Invoice dispatch limit must be between 1 and 100")
    delivered = 0
    try:
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        retry_cutoff = datetime.now(UTC) - timedelta(minutes=10)
        query = (
            select(InvoiceOutbox.id)
            .where(
                InvoiceOutbox.completed_at.is_(None),
                or_(
                    InvoiceOutbox.published_at.is_(None), InvoiceOutbox.published_at < retry_cutoff
                ),
                or_(InvoiceOutbox.locked_at.is_(None), InvoiceOutbox.locked_at < cutoff),
            )
            .order_by(InvoiceOutbox.created_at, InvoiceOutbox.id)
            .limit(limit)
        )
        if outbox_id is not None:
            query = query.where(InvoiceOutbox.id == outbox_id)
        candidates = list(db.exec(query))
        db.rollback()  # Do not retain a SQLite read snapshot while acquiring claims.
        for entry_id in candidates:
            claimed_at = datetime.now(UTC)
            result = db.execute(
                update(InvoiceOutbox)
                .where(
                    InvoiceOutbox.id == entry_id,
                    InvoiceOutbox.completed_at.is_(None),
                    or_(
                        InvoiceOutbox.published_at.is_(None),
                        InvoiceOutbox.published_at < retry_cutoff,
                    ),
                    or_(InvoiceOutbox.locked_at.is_(None), InvoiceOutbox.locked_at < cutoff),
                )
                .values(locked_at=claimed_at, attempts=InvoiceOutbox.attempts + 1)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            if result.rowcount != 1:
                continue
            entry = db.get(InvoiceOutbox, entry_id, populate_existing=True)
            if entry is None:
                continue
            kwargs = {
                "session_id": entry.session_id,
                "device_id": entry.device_id,
                "total_cost": str(entry.total_cost),
            }
            db.rollback()  # Publishing never holds an open database transaction.
            try:
                generate_invoice_task.apply_async(kwargs=kwargs, task_id=entry_id, retry=False)
            except Exception as error:
                # Persist a safe category, never broker URLs or credential-bearing text.
                db.execute(
                    update(InvoiceOutbox)
                    .where(InvoiceOutbox.id == entry_id, InvoiceOutbox.locked_at == claimed_at)
                    .values(locked_at=None, last_error=type(error).__name__[:128])
                )
                db.commit()
                logger.warning(
                    "invoice_broker_unavailable",
                    outbox_id=entry_id,
                    error_type=type(error).__name__,
                )
                break  # A broker outage affects every entry; let the next beat retry.
            db.execute(
                update(InvoiceOutbox)
                .where(InvoiceOutbox.id == entry_id, InvoiceOutbox.locked_at == claimed_at)
                .values(published_at=datetime.now(UTC), locked_at=None, last_error=None)
            )
            db.commit()
            delivered += 1
    except SQLAlchemyError:
        db.rollback()
        logger.exception("invoice_outbox_database_failure")
    return delivered
