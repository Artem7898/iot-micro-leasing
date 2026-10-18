from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Lock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from api.dependencies import db as database
from api.dependencies.db import create_database_engine
from api.models.device import Device, InvoiceOutbox, UsageSession
from api.services.invoice_service import dispatch_pending_invoices
from api.tasks.generate_invoice import generate_invoice_task


@pytest.fixture
def engine(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'outbox.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Device(id="dev_outbox1", rate_per_unit=Decimal("1")))
        db.commit()
        session = UsageSession(device_id="dev_outbox1", is_active=False)
        db.add(session)
        db.commit()
        db.add(
            InvoiceOutbox(
                session_id=session.id, device_id=session.device_id, total_cost=Decimal("3.25")
            )
        )
        db.commit()
    yield engine
    engine.dispose()


def test_delivery_failure_then_retry_is_durable(engine, monkeypatch):
    def fail(**kwargs):
        raise ConnectionError("secret URL must not be stored")

    monkeypatch.setattr(generate_invoice_task, "apply_async", fail)
    with Session(engine) as db:
        assert dispatch_pending_invoices(db) == 0
        pending = db.exec(select(InvoiceOutbox)).one()
        assert pending.attempts == 1 and pending.last_error == "ConnectionError"
        assert pending.locked_at is None and pending.published_at is None
    published = []
    monkeypatch.setattr(
        generate_invoice_task, "apply_async", lambda **kwargs: published.append(kwargs)
    )
    with Session(engine) as db:
        assert dispatch_pending_invoices(db) == 1
        assert dispatch_pending_invoices(db) == 0
        entry = db.exec(select(InvoiceOutbox)).one()
        assert entry.attempts == 2 and entry.published_at is not None
        assert entry.last_error is None
        assert published[0]["task_id"] == entry.id
        assert published[0]["retry"] is False
        assert published[0]["kwargs"]["total_cost"] == "3.2500"


@pytest.mark.parametrize(("lease_age", "delivered"), [(1, 0), (301, 1)])
def test_only_expired_leases_can_be_reclaimed(engine, monkeypatch, lease_age, delivered):
    monkeypatch.setattr(generate_invoice_task, "apply_async", lambda **kwargs: None)
    with Session(engine) as db:
        entry = db.exec(select(InvoiceOutbox)).one()
        entry.locked_at = datetime.now(UTC) - timedelta(seconds=lease_age)
        db.add(entry)
        db.commit()
        assert dispatch_pending_invoices(db) == delivered


def test_concurrent_dispatchers_claim_each_invoice_once(engine, monkeypatch):
    published = []
    lock = Lock()
    barrier = Barrier(8)

    def publish(**kwargs):
        with lock:
            published.append(kwargs)

    monkeypatch.setattr(generate_invoice_task, "apply_async", publish)

    def dispatch(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return dispatch_pending_invoices(db)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(dispatch, range(8)))
    assert sum(results) == 1 and len(published) == 1


@pytest.mark.parametrize(
    ("published_age", "complete", "expected"),
    [(None, True, 0), (60, False, 0), (601, False, 1), (601, True, 0)],
)
def test_unfinished_delivery_retried_after_grace_period(
    engine,
    monkeypatch,
    published_age,
    complete,
    expected,
):
    published = []
    monkeypatch.setattr(
        generate_invoice_task, "apply_async", lambda **kwargs: published.append(kwargs)
    )
    with Session(engine) as db:
        entry = db.exec(select(InvoiceOutbox)).one()
        if published_age is not None:
            entry.published_at = datetime.now(UTC) - timedelta(seconds=published_age)
        if complete:
            entry.completed_at = datetime.now(UTC)
        db.add(entry)
        db.commit()
        assert dispatch_pending_invoices(db) == expected
        assert len(published) == expected


def test_publish_ack_preserves_concurrent_worker_completion(engine, monkeypatch):
    def finish(**kwargs):
        with Session(engine) as worker_db:
            entry = worker_db.get(InvoiceOutbox, kwargs["task_id"])
            entry.completed_at = datetime.now(UTC)
            worker_db.add(entry)
            worker_db.commit()

    monkeypatch.setattr(generate_invoice_task, "apply_async", finish)
    with Session(engine) as db:
        assert dispatch_pending_invoices(db) == 1
        entry = db.exec(select(InvoiceOutbox)).one()
        assert entry.completed_at is not None and entry.published_at is not None
        assert dispatch_pending_invoices(db) == 0


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_dispatch_batch_is_bounded(engine, limit):
    with Session(engine) as db, pytest.raises(ValueError):
        dispatch_pending_invoices(db, limit=limit)


def test_database_rejects_multiple_active_sessions(engine):
    with Session(engine) as db:
        db.add(UsageSession(device_id="dev_outbox1"))
        db.add(UsageSession(device_id="dev_outbox1"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_dependency_returns_connections_and_rolls_back_uncommitted_work(engine, monkeypatch):
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    for _ in range(20):
        dependency = database.get_db_session()
        session = next(dependency)
        session.exec(select(Device)).all()
        dependency.close()
        assert engine.pool.checkedout() == 0
    dependency = database.get_db_session()
    session = next(dependency)
    session.add(Device(id="dev_rollback", rate_per_unit=Decimal("1")))
    session.flush()
    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("request failed"))
    assert engine.pool.checkedout() == 0
    with Session(engine) as db:
        assert db.get(Device, "dev_rollback") is None
