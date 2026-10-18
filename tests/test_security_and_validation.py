from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from starlette.routing import Route

from api.core.config import Settings, settings
from api.core.security import require_api_key
from api.dependencies.db import get_db_session
from api.main import app
from api.schemas.usage import UsagePingRequest


def test_authentication_is_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "API_KEY", None)
    with pytest.raises(HTTPException) as error:
        require_api_key("anything")
    assert error.value.status_code == 503


@pytest.mark.parametrize("supplied", [None, "wrong", "я", "x" * 513])
def test_wrong_api_keys_rejected(monkeypatch, supplied):
    monkeypatch.setattr(settings, "API_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "API_KEY", SecretStr("a" * 32))
    with pytest.raises(HTTPException) as error:
        require_api_key(supplied)
    assert error.value.status_code == 401


def test_correct_key_and_explicit_development_bypass(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", SecretStr("a" * 32))
    monkeypatch.setattr(settings, "API_AUTH_ENABLED", True)
    require_api_key("a" * 32)
    monkeypatch.setattr(settings, "API_AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    require_api_key(None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"API_AUTH_ENABLED": False},
        {"API_KEY": "short"},
        {"DATABASE_URL": "sqlite:///prod.sqlite3"},
        {"AUTO_CREATE_SCHEMA": True},
    ],
)
def test_production_rejects_unsafe_configuration(overrides):
    values = {
        "ENVIRONMENT": "production",
        "API_AUTH_ENABLED": True,
        "API_KEY": "a" * 32,
        "DATABASE_URL": "postgresql+psycopg://localhost/billing",
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_valid_production_configuration_and_hidden_secret():
    configured = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        API_AUTH_ENABLED=True,
        API_KEY="a" * 32,
        DATABASE_URL="postgresql://localhost/billing",
    )
    assert configured.DATABASE_URL.startswith("postgresql+psycopg://")
    assert "a" * 32 not in repr(configured)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        True,
        False,
        None,
        [],
        {},
        "nan",
        "inf",
        "-Infinity",
        "1e100",
        "0.00001",
        "1000000001",
        "secret-value",
        "x" * 1000,
    ],
)
def test_invalid_consumption_rejected(value):
    with pytest.raises(ValidationError):
        UsagePingRequest(
            device_id="dev_test123",
            event_type="usage_tick",
            timestamp=datetime.now(UTC),
            payload={"units_consumed": value},
        )


def test_timestamp_must_be_timezone_aware_and_tick_positive():
    for timestamp, units in [(datetime(2026, 1, 1), 1), (datetime.now(UTC), 0)]:
        with pytest.raises(ValidationError):
            UsagePingRequest(
                device_id="dev_test123",
                event_type="usage_tick",
                timestamp=timestamp,
                payload={"units_consumed": units},
            )


def test_payload_is_bounded_and_decimal_exact():
    event = UsagePingRequest(
        device_id="dev_test123",
        event_type="usage_tick",
        timestamp=datetime.now(UTC),
        payload={"units_consumed": "0.1000"},
    )
    assert event.payload.units_consumed == Decimal("0.1000")
    with pytest.raises(ValidationError):
        UsagePingRequest(
            device_id="dev_test123",
            event_type="session_start",
            timestamp=datetime.now(UTC),
            payload={"unexpected": {"nested": "value"}},
        )


@pytest.fixture
def client():
    def no_database():
        yield None

    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_db_session] = no_database
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_validation_errors_never_echo_raw_payload(client):
    response = client.post(
        "/api/v1/usage/ping",
        headers={"Idempotency-Key": "validation"},
        json={
            "device_id": "dev_test123",
            "event_type": "usage_tick",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"units_consumed": "private-credential"},
        },
    )
    assert response.status_code == 422
    assert "private-credential" not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


def test_request_size_limit_applies_to_chunked_body(client):
    response = client.post("/api/v1/usage/ping", content=iter([b"x" * 10000, b"x" * 10000]))
    assert response.status_code == 413


def test_limiter_outage_returns_safe_service_unavailable(client, monkeypatch):
    from api.core.rate_limit import limiter

    def unavailable(*args, **kwargs):
        import redis

        raise redis.ConnectionError("redis://private-credential@internal")

    monkeypatch.setattr(limiter, "enabled", True)
    monkeypatch.setattr(limiter, "_check_request_limit", unavailable)
    response = client.get("/api/docs")
    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limiting temporarily unavailable"}
    assert "private-credential" not in response.text


def test_request_ids_and_safe_error_boundary(client):
    async def fail(request):
        raise RuntimeError("sensitive-internal-value")

    route = Route("/test-error-boundary", fail)
    app.router.routes.append(route)
    try:
        response = client.get("/test-error-boundary", headers={"X-Request-ID": "request-123"})
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}
        assert response.headers["x-request-id"] == "request-123"
        assert "sensitive-internal-value" not in response.text
    finally:
        app.router.routes.remove(route)


def test_readiness_checks_both_services(client, monkeypatch):
    import api.main as main

    class RedisClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def ping(self):
            return True

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    monkeypatch.setattr(main.redis.Redis, "from_url", lambda *args, **kwargs: RedisClient())
    assert client.get("/ready").json() == {
        "status": "ready",
        "checks": {"database": True, "redis": True},
    }

    def offline(*args, **kwargs):
        raise main.redis.ConnectionError("internal redis URL must not appear")

    monkeypatch.setattr(main.redis.Redis, "from_url", offline)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"] is False
    assert "internal redis URL" not in response.text
    engine.dispose()
