import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import Session, SQLModel, select

from api.dependencies.db import create_database_engine
from api.models.device import Device, UsageEvent, UsageSession
from api.schemas.usage import UsagePingRequest
from api.services.usage_service import UsageService


@pytest.fixture(params=["sqlite", "postgres"])
def concurrent_engine(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL is required for real PostgreSQL concurrency validation")
        base_engine = create_database_engine(url)
        schema = f"test_billing_{uuid4().hex}"
        with base_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = base_engine.execution_options(schema_translate_map={None: schema})
    else:
        base_engine = engine = create_database_engine(
            f"sqlite:///{tmp_path / 'concurrent.sqlite3'}"
        )
        schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(Device(id="dev_parallel", rate_per_unit=Decimal("0.1000")))
            db.commit()
        yield engine
    finally:
        if schema is not None:
            with base_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        base_engine.dispose()


def request_for(event="usage_tick", units="1", device="dev_parallel"):
    return UsagePingRequest.model_validate(
        {
            "device_id": device,
            "event_type": event,
            "timestamp": datetime.now(UTC) - timedelta(seconds=2),
            "payload": {"units_consumed": units},
        }
    )


def run_parallel(engine, requests):
    barrier = Barrier(len(requests))

    def accept(item):
        request, key = item
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                return UsageService(db).process_ping(request, key)
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return list(executor.map(accept, requests))


def test_concurrent_distinct_ticks_do_not_lose_updates(concurrent_engine):
    with Session(concurrent_engine) as db:
        db.add(UsageSession(device_id="dev_parallel"))
        db.commit()
    request = request_for()
    results = run_parallel(concurrent_engine, [(request, f"tick-{index}") for index in range(8)])
    assert all(not isinstance(result, int) for result in results)
    with Session(concurrent_engine) as db:
        assert db.exec(select(UsageSession)).one().total_cost == Decimal("0.8000")
        assert len(db.exec(select(UsageEvent)).all()) == 8


def test_concurrent_replays_bill_once(concurrent_engine):
    with Session(concurrent_engine) as db:
        db.add(UsageSession(device_id="dev_parallel"))
        db.commit()
    request = request_for()
    results = run_parallel(concurrent_engine, [(request, "duplicate") for _ in range(8)])
    assert all(not isinstance(result, int) for result in results)
    assert all(result.model_dump() == results[0].model_dump() for result in results)
    with Session(concurrent_engine) as db:
        assert db.exec(select(UsageSession)).one().total_cost == Decimal("0.1000")
        assert len(db.exec(select(UsageEvent)).all()) == 1


def test_concurrent_starts_create_one_session(concurrent_engine):
    request = request_for("session_start", "0")
    results = run_parallel(concurrent_engine, [(request, f"start-{index}") for index in range(8)])
    assert sum(not isinstance(result, int) for result in results) == 1
    assert results.count(409) == 7
    with Session(concurrent_engine) as db:
        assert len(db.exec(select(UsageSession)).all()) == 1


def test_global_key_collision_rolls_back_losing_device_charge(concurrent_engine):
    with Session(concurrent_engine) as db:
        db.add(Device(id="dev_parallel2", rate_per_unit=Decimal("0.1000")))
        db.commit()
        db.add(UsageSession(device_id="dev_parallel"))
        db.add(UsageSession(device_id="dev_parallel2"))
        db.commit()
    results = run_parallel(
        concurrent_engine,
        [
            (request_for(device="dev_parallel"), "shared"),
            (request_for(device="dev_parallel2"), "shared"),
        ],
    )
    assert sum(not isinstance(result, int) for result in results) == 1
    assert results.count(409) == 1
    with Session(concurrent_engine) as db:
        assert sum(session.total_cost for session in db.exec(select(UsageSession))) == Decimal(
            "0.1000"
        )
        assert len(db.exec(select(UsageEvent)).all()) == 1
