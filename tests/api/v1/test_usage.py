from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, select

from api.core.security import require_api_key
from api.dependencies.db import create_database_engine, get_db_session
from api.main import app
from api.models.device import Device, InvoiceOutbox, UsageEvent, UsageSession
from api.schemas.usage import UsagePingRequest
from api.services.usage_service import UsageService
from api.tasks.generate_invoice import generate_invoice_task


@pytest.fixture
def engine(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'billing.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(engine):
    def override_session():
        with Session(engine) as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    previous_limiter = app.state.limiter.enabled
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[require_api_key] = lambda: None
    app.state.limiter.enabled = False
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous_overrides)
    app.state.limiter.enabled = previous_limiter


def seed_device(db, *, active=True, session=True, cost="0.0000", rate="0.5000"):
    device = Device(id="dev_test1234", rate_per_unit=Decimal(rate), is_active=active)
    db.add(device)
    db.commit()
    if session:
        db.add(UsageSession(device_id=device.id, total_cost=Decimal(cost)))
        db.commit()
    return device


def ping(*, event="usage_tick", units="10", timestamp=None, device="dev_test1234"):
    return {
        "device_id": device,
        "event_type": event,
        "timestamp": (timestamp or datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
        "payload": {"units_consumed": units},
    }


def post(client, payload, key=None):
    return client.post(
        "/api/v1/usage/ping", json=payload, headers={"Idempotency-Key": key or str(uuid4())}
    )


def test_usage_ping_happy_path(client, db):
    seed_device(db)
    response = post(client, ping())
    assert response.status_code == 200
    assert response.json()["total_cost_usd"] == "5.0000"
    assert response.json()["status"] == "active"
    db.expire_all()
    assert db.exec(select(UsageSession)).one().total_cost == Decimal("5.0000")
    assert len(db.exec(select(UsageEvent)).all()) == 1


@pytest.mark.parametrize(("active", "expected"), [(True, 404), (False, 403)])
def test_unknown_or_inactive_device(client, db, active, expected):
    if not active:
        seed_device(db, active=False)
    response = post(client, ping())
    assert response.status_code == expected
    assert db.exec(select(UsageEvent)).first() is None


@pytest.mark.parametrize(("offset", "detail"), [(-301, "too old"), (30, "future")])
def test_timestamp_window(client, db, offset, detail):
    seed_device(db)
    response = post(client, ping(timestamp=datetime.now(UTC) + timedelta(seconds=offset)))
    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert db.exec(select(UsageSession)).one().total_cost == 0


@pytest.mark.parametrize(
    "units", ["-1", "NaN", "Infinity", "-Infinity", True, {}, [], "0.00001", "1000000001", "0"]
)
def test_invalid_units_are_rejected_before_billing(client, db, units):
    seed_device(db)
    response = post(client, ping(units=units))
    assert response.status_code == 422
    assert db.exec(select(UsageEvent)).first() is None
    assert db.exec(select(UsageSession)).one().total_cost == 0


@pytest.mark.parametrize(
    "mutation", ["missing_units", "naive_time", "extra_payload", "extra_field", "invalid_device"]
)
def test_invalid_request_shapes(client, db, mutation):
    seed_device(db)
    payload = ping()
    if mutation == "missing_units":
        payload["payload"] = {}
    elif mutation == "naive_time":
        payload["timestamp"] = datetime.now().isoformat()
    elif mutation == "extra_payload":
        payload["payload"]["untrusted"] = "value"
    elif mutation == "extra_field":
        payload["arbitrary"] = "value"
    else:
        payload["device_id"] = "../../etc/passwd"
    assert post(client, payload).status_code == 422


@pytest.mark.parametrize("key", [None, "", "a" * 129, "two words"])
def test_idempotency_header_required_and_bounded(client, db, key):
    seed_device(db)
    headers = {} if key is None else {"Idempotency-Key": key}
    response = client.post("/api/v1/usage/ping", json=ping(), headers=headers)
    assert response.status_code == 422


def test_replay_returns_original_balance_without_rebilling(client, db):
    seed_device(db)
    payload = ping()
    first = post(client, payload, "original")
    later = post(client, ping(units="2"), "later")
    replay = post(client, payload, "original")
    assert first.status_code == later.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    db.expire_all()
    assert db.exec(select(UsageSession)).one().total_cost == Decimal("6.0000")
    assert len(db.exec(select(UsageEvent)).all()) == 2


def test_replay_with_changed_payload_conflicts(client, db):
    seed_device(db)
    payload = ping()
    assert post(client, payload, "same-key").status_code == 200
    payload["payload"]["units_consumed"] = "11"
    assert post(client, payload, "same-key").status_code == 409
    db.expire_all()
    assert db.exec(select(UsageSession)).one().total_cost == Decimal("5.0000")


def test_semantically_equal_units_share_fingerprint(client, db):
    seed_device(db)
    payload = ping(units="10.0")
    first = post(client, payload, "same-key")
    payload["payload"]["units_consumed"] = "10.0000"
    assert post(client, payload, "same-key").json() == first.json()


def test_replay_survives_device_disable_and_time_window(client, db, monkeypatch):
    from api.services import usage_service

    device = seed_device(db)
    payload = ping()
    first = post(client, payload, "same-key")
    device.is_active = False
    db.add(device)
    db.commit()

    class LaterClock:
        @staticmethod
        def now(tz):
            return datetime.now(tz) + timedelta(hours=1)

    monkeypatch.setattr(usage_service, "datetime", LaterClock)
    assert post(client, payload, "same-key").json() == first.json()


@pytest.mark.parametrize("event", ["usage_tick", "session_end"])
def test_no_active_session_is_conflict(client, db, event):
    seed_device(db, session=False)
    assert post(client, ping(event=event)).status_code == 409


def test_start_does_not_silently_replace_uninvoiced_session(client, db):
    seed_device(db, cost="9.0000")
    assert post(client, ping(event="session_start", units="0")).status_code == 409
    session = db.exec(select(UsageSession)).one()
    assert session.is_active and session.total_cost == Decimal("9.0000")
    assert db.exec(select(InvoiceOutbox)).first() is None


def test_lifecycle_and_one_invoice_per_close(client, db, monkeypatch):
    seed_device(db, session=False)
    published = []
    monkeypatch.setattr(
        generate_invoice_task, "apply_async", lambda **kwargs: published.append(kwargs)
    )
    base = datetime.now(UTC) - timedelta(seconds=30)
    started = post(client, ping(event="session_start", units="0", timestamp=base), "start")
    ended_payload = ping(event="session_end", units="2", timestamp=base + timedelta(seconds=1))
    closed = post(client, ended_payload, "end")
    replay = post(client, ended_payload, "end")
    reopened = post(
        client,
        ping(event="session_start", units="0", timestamp=base + timedelta(seconds=2)),
        "reopen",
    )
    assert (
        started.status_code
        == closed.status_code
        == replay.status_code
        == reopened.status_code
        == 200
    )
    assert closed.json() == replay.json()
    assert closed.json()["total_cost_usd"] == "1.0000"
    assert closed.json()["status"] == "closed"
    assert started.json()["session_id"] != reopened.json()["session_id"]
    assert len(published) == 1
    assert len(db.exec(select(InvoiceOutbox)).all()) == 1


def test_broker_failure_keeps_billing_and_outbox_durable(client, db, monkeypatch):
    seed_device(db)

    def fail(**kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(generate_invoice_task, "apply_async", fail)
    payload = ping(event="session_end", units="4")
    response = post(client, payload, "closing")
    assert response.status_code == 200
    assert "pending" in response.json()["message"]
    assert post(client, payload, "closing").json() == response.json()
    db.expire_all()
    session = db.exec(select(UsageSession)).one()
    outbox = db.exec(select(InvoiceOutbox)).one()
    assert not session.is_active and session.total_cost == Decimal("2.0000")
    assert outbox.published_at is None and outbox.locked_at is None
    assert outbox.attempts == 1 and outbox.last_error == "ConnectionError"


def test_dispatch_database_ack_failure_keeps_immutable_receipt(client, db, monkeypatch):
    seed_device(db)
    original_commit = Session.commit
    commits = 0

    def fail_second_commit(session):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OperationalError("COMMIT", {}, RuntimeError("outbox acknowledgement unavailable"))
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_second_commit)
    payload = ping(event="session_end", units="4")
    response = post(client, payload, "immutable-close")
    replay = post(client, payload, "immutable-close")
    assert response.status_code == replay.status_code == 200
    assert response.json() == replay.json()
    assert "pending" in response.json()["message"]
    db.expire_all()
    assert db.exec(select(UsageSession)).one().total_cost == Decimal("2.0000")
    assert db.exec(select(InvoiceOutbox)).one().published_at is None


def test_out_of_order_timestamp_rejected_without_debt_change(client, db):
    seed_device(db)
    now = datetime.now(UTC) - timedelta(seconds=10)
    assert post(client, ping(timestamp=now)).status_code == 200
    assert post(client, ping(timestamp=now - timedelta(seconds=1))).status_code == 409
    db.expire_all()
    assert db.exec(select(UsageSession)).one().total_cost == Decimal("5.0000")


def test_rounding_and_maximum_debt_are_explicit(client, db):
    seed_device(db, rate="0.0001")
    assert post(client, ping(units="0.5")).json()["total_cost_usd"] == "0.0000"
    assert post(client, ping(units="1.5")).json()["total_cost_usd"] == "0.0002"
    db.expire_all()
    session = db.exec(select(UsageSession)).one()
    session.total_cost = Decimal("99999999.9999")
    db.add(session)
    db.commit()
    assert post(client, ping(units="1")).status_code == 409
    db.refresh(session)
    assert session.total_cost == Decimal("99999999.9999")


@pytest.mark.parametrize(
    ("active", "session", "status", "debt"),
    [
        (True, True, 200, "3.2500"),
        (True, False, 200, "0.0000"),
        (False, True, 403, None),
    ],
)
def test_balance_endpoint(client, db, active, session, status, debt):
    seed_device(db, active=active, session=session, cost="3.2500")
    response = client.get("/api/v1/devices/dev_test1234/balance")
    assert response.status_code == status
    if debt is not None:
        assert response.json()["active_session_debt_usd"] == debt


def test_unknown_device_balance(client):
    assert client.get("/api/v1/devices/dev_ghost/balance").status_code == 404


@pytest.mark.parametrize("event", ["session_start", "usage_tick", "session_end"])
def test_failed_commit_rolls_back_all_financial_side_effects(db, monkeypatch, event):
    from fastapi import HTTPException

    seed_device(db, session=event != "session_start")
    published = []
    monkeypatch.setattr(
        generate_invoice_task, "apply_async", lambda **kwargs: published.append(kwargs)
    )

    def fail_commit():
        raise OperationalError("COMMIT", {}, RuntimeError("simulated database outage"))

    monkeypatch.setattr(db, "commit", fail_commit)
    request = UsagePingRequest.model_validate(ping(event=event))
    with pytest.raises(HTTPException) as caught:
        UsageService(db).process_ping(request, "must-rollback")
    assert caught.value.status_code == 503
    assert db.exec(select(UsageEvent)).first() is None
    assert db.exec(select(InvoiceOutbox)).first() is None
    sessions = db.exec(select(UsageSession)).all()
    if event == "session_start":
        assert sessions == []
    else:
        assert len(sessions) == 1 and sessions[0].is_active and sessions[0].total_cost == 0
    assert published == []
