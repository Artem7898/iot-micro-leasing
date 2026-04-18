import os
from decimal import Decimal
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from api.core.celery_app import celery_app
import structlog

logger = structlog.get_logger(__name__)


@celery_app.task(name="api.tasks.generate_invoice")
def generate_invoice_task(session_id: str, device_id: str, total_cost: Decimal):
    """
    Фоновая задача. Генерирует PDF-счет и сохраняет его в папку /invoices.
    В проде здесь был бы upload в AWS S3.
    """
    logger.info("starting_pdf_generation", session_id=session_id, cost=total_cost)

    # Создаем папку, если её нет
    os.makedirs("invoices", exist_ok=True)
    filepath = f"invoices/invoice_{session_id}.pdf"

    # Генерация PDF
    c = canvas.Canvas(filepath, pagesize=letter)
    c.drawString(100, 750, "IoT Micro-Leasing - Invoice")
    c.drawString(100, 700, f"Session ID: {session_id}")
    c.drawString(100, 680, f"Device ID: {device_id}")
    c.drawString(100, 660, f"Total to pay: ${float(total_cost):.2f}")
    c.save()

    logger.info("pdf_generated_successfully", filepath=filepath)
    return {"status": "success", "path": filepath}