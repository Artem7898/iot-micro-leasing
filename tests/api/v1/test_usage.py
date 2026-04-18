import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, SQLModel
from sqlmodel.pool import StaticPool
from datetime import datetime, timezone
from decimal import Decimal

from api.main import app
from api.dependencies.db import get_db_session
from api.models.device import Device, UsageSession


@pytest.fixture(name="db")
def session_fixture():
    """In-memory DB для изолированного теста"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(db: Session):
    """Подмена зависимости БД и отключение Rate Limiter"""

    def get_session_override():
        yield db

    app.dependency_overrides[get_db_session] = get_session_override

    # Отключаем Redis Rate Limiting для тестов!
    app.state.limiter.enabled = False

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_usage_ping_happy_path(client: TestClient, db: Session):
    # Arrange
    device = Device(id="dev_test1234", rate_per_unit=Decimal("0.5"), is_active=True)
    db.add(device)

    # Создаем активную сессию для устройства (имитируем, что device уже послал session_start)
    active_session = UsageSession(device_id="dev_test1234")
    db.add(active_session)

    db.commit()

    payload = {
        "device_id": "dev_test1234",
        "event_type": "usage_tick",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"units_consumed": 10.0}
    }

    # Act
    response = client.post("/api/v1/usage/ping", json=payload)

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["total_cost_usd"] == 5.0  # 10 units * 0.5 rate
    assert data["status"] == "active"

    # Проверяем, что сессия осталась той же и стоимость записалась в БД
    db.refresh(active_session)
    assert float(active_session.total_cost) == 5.0


def test_usage_ping_device_not_found(client: TestClient):
    payload = {
        "device_id": "dev_ghost",
        "event_type": "usage_tick",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {}
    }
    response = client.post("/api/v1/usage/ping", json=payload)
    assert response.status_code == 404


def test_usage_ping_stale_request_rejection(client: TestClient, db: Session):
    device = Device(id="dev_test1234", rate_per_unit=Decimal("0.5"), is_active=True)
    db.add(device)
    db.commit()

    old_timestamp = "2020-01-01T12:00:00Z"
    payload = {
        "device_id": "dev_test1234",
        "event_type": "usage_tick",
        "timestamp": old_timestamp,
        "payload": {"units_consumed": 10.0}
    }
    response = client.post("/api/v1/usage/ping", json=payload)
    assert response.status_code == 400
    assert "too old" in response.json()["detail"]