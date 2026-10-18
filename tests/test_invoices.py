"""Invoice security, exact decimal rendering, and retry safety regressions."""

import base64
import re
import stat
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery.exceptions import Retry

from api.core.celery_app import celery_app
from api.tasks import generate_invoice as invoices


@pytest.fixture
def invoice_id() -> str:
    return str(uuid4())


@pytest.fixture(autouse=True)
def isolated_invoice_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(invoices, "settings", SimpleNamespace(INVOICE_DIR=tmp_path))


@pytest.fixture
def outbox_database(monkeypatch, tmp_path, invoice_id):
    from sqlmodel import Session, SQLModel

    from api.dependencies import db as database
    from api.models.device import Device, InvoiceOutbox, UsageSession

    engine = database.create_database_engine(f"sqlite:///{tmp_path / 'invoices.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    outbox_id = str(uuid4())
    with Session(engine) as db:
        db.add(Device(id="dev_invoice1", rate_per_unit=Decimal("1")))
        db.commit()
        db.add(UsageSession(id=invoice_id, device_id="dev_invoice1", is_active=False))
        db.commit()
        db.add(
            InvoiceOutbox(
                id=outbox_id,
                session_id=invoice_id,
                device_id="dev_invoice1",
                total_cost=Decimal("1.0000"),
                published_at=datetime.now(UTC),
                last_error="PreviousStorageFailure",
            )
        )
        db.commit()
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    yield engine, outbox_id
    engine.dispose()


def _page_text(pdf: bytes) -> bytes:
    """Decode ReportLab's page stream without a second PDF dependency."""
    match = re.search(rb"stream\r?\n(.*?)endstream", pdf, flags=re.DOTALL)
    assert match is not None
    encoded = match.group(1).strip()
    assert encoded.endswith(b"~>")
    return zlib.decompress(base64.a85decode(encoded[:-2]))


@pytest.mark.parametrize(
    ("amount", "display"),
    [
        ("0", b"$0.0000"),
        ("-0", b"$0.0000"),
        ("0.0001", b"$0.0001"),
        ("0.1234", b"$0.1234"),
        (Decimal("99999999.9999"), b"$99999999.9999"),
        ("1.23000", b"$1.2300"),
    ],
)
def test_pdf_preserves_exact_billing_precision(tmp_path, invoice_id, amount, display):
    destination = invoices.render_invoice(invoice_id, "device-1", amount, tmp_path)
    pdf = destination.read_bytes()
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert display in _page_text(pdf)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "/tmp/escape", "a/../../escape", "", "a" * 10_000, None, 123],
)
def test_invalid_session_id_never_creates_a_directory(tmp_path, session_id):
    destination = tmp_path / "invoices"
    with pytest.raises(ValueError, match="UUID"):
        invoices.render_invoice(session_id, "device-1", "1.0000", destination)
    assert not destination.exists()


def test_uuid_is_canonicalized_for_the_filename(tmp_path, invoice_id):
    destination = invoices.render_invoice(invoice_id.upper(), "device-1", "1", tmp_path)
    assert destination.name == f"invoice_{invoice_id}.pdf"


@pytest.mark.parametrize("device_id", ["", " ", "dev\nforged", "dev\x00", "a" * 37, None])
def test_invalid_device_payload_is_rejected(tmp_path, invoice_id, device_id):
    with pytest.raises(ValueError, match="device ID"):
        invoices.render_invoice(invoice_id, device_id, "1.0000", tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "amount",
    [
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "-0.0001",
        "100000000",
        "1.00001",
        "1e1000000",
        "9" * 1000,
        "bad-number",
        None,
        True,
        0.1,
    ],
)
def test_invalid_money_is_rejected_before_creating_files(tmp_path, invoice_id, amount):
    with pytest.raises(ValueError, match="Invoice total"):
        invoices.render_invoice(invoice_id, "device-1", amount, tmp_path)
    assert not list(tmp_path.iterdir())


def test_retries_and_concurrent_duplicate_delivery_produce_one_complete_pdf(tmp_path, invoice_id):
    def render(_):
        return invoices.render_invoice(invoice_id, "device-1", "0.1234", tmp_path)

    expected = render(None).read_bytes()
    with ThreadPoolExecutor(max_workers=8) as pool:
        destinations = list(pool.map(render, range(32)))
    assert len(set(destinations)) == 1
    assert destinations[0].read_bytes() == expected
    assert list(tmp_path.iterdir()) == [destinations[0]]


def test_destination_symlink_cannot_overwrite_another_file(tmp_path, invoice_id):
    protected = tmp_path / "protected.txt"
    protected.write_text("keep me", encoding="utf-8")
    invoice_dir = tmp_path / "invoices"
    invoice_dir.mkdir()
    destination = invoice_dir / f"invoice_{invoice_id}.pdf"
    destination.symlink_to(protected)
    assert invoices.render_invoice(invoice_id, "device-1", "1", invoice_dir) == destination
    assert protected.read_text(encoding="utf-8") == "keep me"
    assert not destination.is_symlink()
    assert destination.read_bytes().startswith(b"%PDF-")


@pytest.mark.parametrize("failure_point", ["save", "fsync", "replace"])
def test_failed_write_keeps_previous_pdf_and_cleans_temporary_files(
    tmp_path, invoice_id, monkeypatch, failure_point
):
    destination = invoices.render_invoice(invoice_id, "device-1", "1", tmp_path)
    original = destination.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private storage detail")

    if failure_point == "save":
        monkeypatch.setattr(invoices.canvas.Canvas, "save", fail)
    else:
        monkeypatch.setattr(invoices.os, failure_point, fail)
    with pytest.raises(OSError):
        invoices.render_invoice(invoice_id, "device-1", "2", tmp_path)
    assert destination.read_bytes() == original
    assert list(tmp_path.iterdir()) == [destination]


def test_atomic_publish_only_exposes_a_finished_document(tmp_path, invoice_id, monkeypatch):
    real_replace = invoices.os.replace
    destination = tmp_path / f"invoice_{invoice_id}.pdf"

    def inspect_then_replace(source, target):
        assert not destination.exists()
        temporary_bytes = Path(source).read_bytes()
        assert temporary_bytes.rstrip().endswith(b"%%EOF")
        return real_replace(source, target)

    monkeypatch.setattr(invoices.os, "replace", inspect_then_replace)
    invoices.render_invoice(invoice_id, "device-1", "1", tmp_path)
    assert destination.exists()


@pytest.mark.parametrize(("attempt", "delay"), [(0, 5), (1, 10), (2, 20)])
def test_task_retries_storage_errors_without_exposing_private_details(
    monkeypatch, invoice_id, attempt, delay
):
    def fail(*args, **kwargs):
        raise OSError("private-storage-path")

    monkeypatch.setattr(invoices, "render_invoice", fail)
    task = invoices.generate_invoice_task
    task.push_request(retries=attempt, called_directly=False, is_eager=True)
    try:
        with pytest.raises(Retry) as error:
            task.run(invoice_id, "device-1", "1")
    finally:
        task.pop_request()
    assert error.value.when == delay
    assert "private-storage-path" not in str(error.value)
    assert str(error.value.exc) == "Invoice storage is temporarily unavailable."


def test_task_stops_after_three_storage_retries(monkeypatch, invoice_id):
    def fail(*args, **kwargs):
        raise OSError("private-storage-path")

    monkeypatch.setattr(invoices, "render_invoice", fail)
    task = invoices.generate_invoice_task
    task.push_request(retries=3, called_directly=False, is_eager=True)
    try:
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            task.run(invoice_id, "device-1", "1")
    finally:
        task.pop_request()


def test_invalid_broker_payload_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(invoices.settings, "INVOICE_DIR", tmp_path)
    with pytest.raises(ValueError, match="UUID"):
        invoices.generate_invoice_task.run("../escape", "device-1", "1")
    assert not list(tmp_path.iterdir())


def test_task_completion_is_idempotent_and_preserves_publication(outbox_database, invoice_id):
    from sqlmodel import Session

    from api.models.device import InvoiceOutbox

    engine, outbox_id = outbox_database
    task = invoices.generate_invoice_task
    task.push_request(id=outbox_id)
    try:
        task.run(invoice_id, "dev_invoice1", "1")
        with Session(engine) as db:
            entry = db.get(InvoiceOutbox, outbox_id)
            completed_at, published_at = entry.completed_at, entry.published_at
            assert completed_at is not None
            assert entry.last_error is None
        task.run(invoice_id, "dev_invoice1", "1")
        with Session(engine) as db:
            entry = db.get(InvoiceOutbox, outbox_id)
            assert entry.completed_at == completed_at
            assert entry.published_at == published_at
        assert engine.pool.checkedout() == 0
    finally:
        task.pop_request()


@pytest.mark.parametrize("mismatch", ["outbox", "session"])
def test_task_cannot_complete_a_different_outbox(outbox_database, invoice_id, mismatch):
    from sqlmodel import Session

    from api.models.device import InvoiceOutbox

    engine, outbox_id = outbox_database
    task = invoices.generate_invoice_task
    task.push_request(id=str(uuid4()) if mismatch == "outbox" else outbox_id)
    try:
        task.run(str(uuid4()) if mismatch == "session" else invoice_id, "dev_invoice1", "1")
        with Session(engine) as db:
            assert db.get(InvoiceOutbox, outbox_id).completed_at is None
        assert engine.pool.checkedout() == 0
    finally:
        task.pop_request()


def test_legacy_hex_session_id_marks_its_original_outbox_complete(outbox_database):
    from sqlmodel import Session

    from api.models.device import InvoiceOutbox, UsageSession

    engine, _ = outbox_database
    legacy_id = uuid4().hex
    outbox_id = str(uuid4())
    with Session(engine) as db:
        db.add(UsageSession(id=legacy_id, device_id="dev_invoice1", is_active=False))
        db.commit()
        db.add(
            InvoiceOutbox(
                id=outbox_id,
                session_id=legacy_id,
                device_id="dev_invoice1",
                total_cost=Decimal("1"),
            )
        )
        db.commit()
    task = invoices.generate_invoice_task
    task.push_request(id=outbox_id)
    try:
        result = task.run(legacy_id, "dev_invoice1", "1")
        assert Path(result["path"]).name == f"invoice_{str(invoices.UUID(legacy_id))}.pdf"
        with Session(engine) as db:
            assert db.get(InvoiceOutbox, outbox_id).completed_at is not None
    finally:
        task.pop_request()


def test_completion_commit_failure_rolls_back_and_returns_connection(
    outbox_database, invoice_id, monkeypatch, tmp_path
):
    import sqlmodel
    from sqlalchemy.exc import SQLAlchemyError

    from api.models.device import InvoiceOutbox

    engine, outbox_id = outbox_database
    real_session = sqlmodel.Session

    class FailingSession(real_session):
        def commit(self):
            raise SQLAlchemyError("private-database-credentials")

    task = invoices.generate_invoice_task
    task.push_request(id=outbox_id, retries=0, called_directly=False, is_eager=True)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlmodel, "Session", FailingSession)
            with pytest.raises(Retry) as error:
                task.run(invoice_id, "dev_invoice1", "1")
        assert str(error.value.exc) == "Invoice completion is temporarily unavailable."
        assert "private-database-credentials" not in str(error.value)
        assert (tmp_path / f"invoice_{invoice_id}.pdf").exists()
        assert engine.pool.checkedout() == 0
        with real_session(engine) as db:
            assert db.get(InvoiceOutbox, outbox_id).completed_at is None
    finally:
        task.pop_request()


def test_exhausted_storage_retries_recover_by_redispatching_incomplete_invoice(
    outbox_database, invoice_id, monkeypatch
):
    from sqlmodel import Session

    from api.models.device import InvoiceOutbox
    from api.services.invoice_service import dispatch_pending_invoices

    engine, outbox_id = outbox_database
    task = invoices.generate_invoice_task

    def fail(*args, **kwargs):
        raise OSError("storage temporarily full")

    task.push_request(id=outbox_id, retries=3, called_directly=False, is_eager=True)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(invoices, "render_invoice", fail)
            with pytest.raises(RuntimeError, match="temporarily unavailable"):
                task.run(invoice_id, "dev_invoice1", "1")
    finally:
        task.pop_request()
    with Session(engine) as db:
        entry = db.get(InvoiceOutbox, outbox_id)
        assert entry.completed_at is None
        entry.published_at = datetime.now(UTC) - timedelta(minutes=11)
        db.add(entry)
        db.commit()

    def publish_and_complete(*, kwargs, task_id, retry):
        # Complete before the publisher acknowledgement, as a fast worker can.
        task.push_request(id=task_id)
        try:
            return task.run(**kwargs)
        finally:
            task.pop_request()

    monkeypatch.setattr(task, "apply_async", publish_and_complete)
    with Session(engine) as db:
        assert dispatch_pending_invoices(db) == 1
        entry = db.get(InvoiceOutbox, outbox_id, populate_existing=True)
        assert entry.completed_at is not None
        assert entry.published_at is not None
        assert entry.last_error is None
        assert dispatch_pending_invoices(db) == 0
    assert engine.pool.checkedout() == 0


def test_periodic_outbox_dispatch_closes_its_database_connection(monkeypatch):
    from sqlalchemy import event
    from sqlmodel import Session, create_engine
    from sqlmodel.pool import StaticPool

    from api.dependencies import db as database
    from api.services import invoice_service

    engine = create_engine("sqlite://", poolclass=StaticPool)
    checked_in = []
    event.listen(engine, "checkin", lambda *_: checked_in.append(True))
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    def dispatch(db, *, limit):
        assert isinstance(db, Session)
        assert limit == 100
        db.connection()
        return 2

    monkeypatch.setattr(invoice_service, "dispatch_pending_invoices", dispatch)
    try:
        assert invoices.dispatch_pending_invoices_task.run() == 2
        assert len(checked_in) == 1
    finally:
        engine.dispose()


def test_periodic_outbox_database_failure_closes_connection_and_sanitizes_retry(monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
    from sqlmodel import create_engine
    from sqlmodel.pool import StaticPool

    from api.dependencies import db as database
    from api.services import invoice_service

    engine = create_engine("sqlite://", poolclass=StaticPool)
    checked_in = []
    event.listen(engine, "checkin", lambda *_: checked_in.append(True))
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    def fail(db, *, limit):
        db.connection()
        raise SQLAlchemyError("private-database-credentials")

    monkeypatch.setattr(invoice_service, "dispatch_pending_invoices", fail)
    task = invoices.dispatch_pending_invoices_task
    task.push_request(retries=0, called_directly=False, is_eager=True)
    try:
        with pytest.raises(Retry) as error:
            task.run()
        assert len(checked_in) == 1
        assert "private-database-credentials" not in str(error.value)
        assert str(error.value.exc) == "Invoice outbox is temporarily unavailable."
    finally:
        task.pop_request()
        engine.dispose()


def test_worker_discovery_and_untrusted_message_formats():
    celery_app.loader.import_default_modules()
    assert "api.tasks.generate_invoice" in celery_app.tasks
    assert "api.tasks.dispatch_pending_invoices" in celery_app.tasks
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.worker_concurrency == 2
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_time_limit > celery_app.conf.task_soft_time_limit
    schedule = celery_app.conf.beat_schedule["deliver-pending-invoices"]
    assert schedule["task"] == "api.tasks.dispatch_pending_invoices"


def test_worker_ignores_unrelated_django_configuration(tmp_path):
    """A Django shell's environment must not initialize the standalone worker ORM."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    process_env = dict(
        os.environ,
        DJANGO_SETTINGS_MODULE="unrelated_project.settings",
        ENVIRONMENT="test",
        PYTHONPYCACHEPREFIX=str(tmp_path / "pycache"),
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from api.core.celery_app import celery_app; celery_app.loader.import_default_modules()",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=process_env,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode()
