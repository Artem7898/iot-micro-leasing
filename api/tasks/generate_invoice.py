"""Idempotent PDF generation and periodic transactional outbox delivery."""

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import structlog
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy.exc import SQLAlchemyError

from api.core.celery_app import celery_app
from api.core.config import settings

logger = structlog.get_logger(__name__)

_COST_QUANTUM = Decimal("0.0001")
_MAX_COST = Decimal("99999999.9999")


def _validate_invoice(
    session_id: str, device_id: str, total_cost: str | Decimal
) -> tuple[str, str, Decimal]:
    """Reject malformed broker payloads before touching the filesystem."""
    if not isinstance(session_id, str) or len(session_id) > 36:
        raise ValueError("Invoice session ID must be a UUID.")
    try:
        canonical_session_id = str(UUID(session_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invoice session ID must be a UUID.") from exc

    if (
        not isinstance(device_id, str)
        or not device_id.strip()
        or len(device_id) > 36
        or not device_id.isprintable()
    ):
        raise ValueError("Invoice device ID must contain 1 to 36 printable characters.")

    if not isinstance(total_cost, (str, Decimal)) or len(str(total_cost)) > 64:
        raise ValueError("Invoice total must be a decimal monetary amount.")
    try:
        amount = Decimal(total_cost)
        if not amount.is_finite() or not Decimal(0) <= amount <= _MAX_COST:
            raise ValueError("Invoice total is outside the supported range.")
        quantized = amount.quantize(_COST_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError("Invoice total must be a decimal monetary amount.") from exc
    if amount != quantized:
        raise ValueError("Invoice total supports at most four decimal places.")
    if quantized.is_zero():
        quantized = Decimal("0.0000")
    return canonical_session_id, device_id, quantized


def render_invoice(
    session_id: str,
    device_id: str,
    total_cost: str | Decimal,
    invoice_dir: Path,
) -> Path:
    """Write a complete, private PDF atomically; concurrent retries are harmless.

    Only the configured directory and a canonical UUID determine the path. Every
    retry renders the same content and replaces the destination atomically, so a
    reader can never observe a partially written invoice.
    """
    session_id, device_id, amount = _validate_invoice(session_id, device_id, total_cost)
    directory = invoice_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / f"invoice_{session_id}.pdf"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix=".invoice-", suffix=".tmp", dir=directory, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            document = canvas.Canvas(temporary, pagesize=letter, pageCompression=1, invariant=1)
            document.setTitle("IoT Micro-Leasing Invoice")
            document.drawString(100, 750, "IoT Micro-Leasing - Invoice")
            document.drawString(100, 700, f"Session ID: {session_id}")
            document.drawString(100, 680, f"Device ID: {device_id}")
            document.drawString(100, 660, f"Total to pay: ${amount:.4f}")
            document.save()
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _mark_invoice_complete(outbox_id: str | None, session_id: str) -> None:
    """Acknowledge rendering separately from broker publication.

    An interrupted worker can repeat this update without changing the original
    completion timestamp. Direct invocation without an outbox task ID is safe.
    """
    if not outbox_id:
        return
    from sqlalchemy import update
    from sqlmodel import Session

    from api.dependencies.db import get_engine
    from api.models.device import InvoiceOutbox

    with Session(get_engine()) as db:
        db.execute(
            update(InvoiceOutbox)
            .where(
                InvoiceOutbox.id == outbox_id,
                InvoiceOutbox.session_id == session_id,
                InvoiceOutbox.completed_at.is_(None),
            )
            .values(completed_at=datetime.now(UTC), last_error=None)
        )
        db.commit()


@celery_app.task(
    bind=True,
    name="api.tasks.generate_invoice",
    max_retries=3,
    default_retry_delay=5,
)
def generate_invoice_task(self, session_id: str, device_id: str, total_cost: str):
    """Create a PDF from a committed outbox message, with bounded I/O retries."""
    try:
        filepath = render_invoice(session_id, device_id, total_cost, settings.INVOICE_DIR)
        _mark_invoice_complete(self.request.id, session_id)
    except ValueError:
        logger.warning("invoice_payload_rejected")
        raise
    except OSError:
        logger.warning("invoice_write_failed", retry=self.request.retries)
        # Do not attach the original exception, whose text may contain paths.
        raise self.retry(
            exc=RuntimeError("Invoice storage is temporarily unavailable."),
            countdown=min(5 * 2**self.request.retries, 60),
        ) from None
    except SQLAlchemyError:
        logger.warning("invoice_completion_database_unavailable", retry=self.request.retries)
        raise self.retry(
            exc=RuntimeError("Invoice completion is temporarily unavailable."),
            countdown=min(5 * 2**self.request.retries, 60),
        ) from None
    logger.info("invoice_generated")
    return {"status": "success", "path": str(filepath)}


@celery_app.task(
    bind=True,
    name="api.tasks.dispatch_pending_invoices",
    max_retries=3,
    default_retry_delay=5,
)
def dispatch_pending_invoices_task(self):
    """Recover broker outages using the durable, committed invoice outbox."""
    from sqlmodel import Session

    from api.dependencies.db import get_engine
    from api.services.invoice_service import dispatch_pending_invoices

    try:
        with Session(get_engine()) as db:
            return dispatch_pending_invoices(db, limit=100)
    except SQLAlchemyError:
        logger.warning("invoice_outbox_database_unavailable", retry=self.request.retries)
        raise self.retry(
            exc=RuntimeError("Invoice outbox is temporarily unavailable."),
            countdown=min(5 * 2**self.request.retries, 60),
        ) from None
