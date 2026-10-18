"""Exercise explicit migrations against fresh and representative legacy databases."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from sqlalchemy.engine import make_url

CONFIG_PATH = Path(__file__).resolve().parents[1] / "alembic.ini"
SESSION_ID = "1e3190c7-223d-4e87-b37a-d2d94fb29317"


def migration_config(database_url: str) -> Config:
    config = Config(str(CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migration.sqlite3'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[sa.Engine]:
    engine = sa.create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def create_legacy(
    engine: sa.Engine,
    *,
    session_id: str = SESSION_ID,
    native_uuid: bool = False,
    device_created_at: bool = False,
) -> None:
    metadata = sa.MetaData()
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rate_per_unit", sa.Numeric(10, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    ]
    if device_created_at:
        columns.append(sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    device = sa.Table("device", metadata, *columns)
    session = sa.Table(
        "usagesession",
        metadata,
        sa.Column("id", sa.Uuid() if native_uuid else sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(36), sa.ForeignKey("device.id"), nullable=False),
        sa.Column("total_cost", sa.Numeric(12, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_ping_at", sa.DateTime(timezone=True)),
    )
    sa.Index("usagesession_device_id_7aae96ad", session.c.device_id)
    metadata.create_all(engine)
    with engine.begin() as connection:
        values = {"id": "dev_migration1", "rate_per_unit": Decimal("0.1250"), "is_active": True}
        if device_created_at:
            values["created_at"] = datetime(2024, 1, 1, tzinfo=UTC)
        connection.execute(device.insert().values(**values))
        connection.execute(
            session.insert().values(
                id=UUID(session_id) if native_uuid else session_id,
                device_id="dev_migration1",
                total_cost=Decimal("123.4567"),
                is_active=True,
                last_ping_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )


def assert_current_schema(database_url: str, engine: sa.Engine) -> None:
    inspector = sa.inspect(engine)
    assert {"device", "usagesession", "usageevent", "invoiceoutbox", "alembic_version"} <= set(
        inspector.get_table_names()
    )
    assert {column["name"] for column in inspector.get_columns("invoiceoutbox")} >= {
        "completed_at",
        "published_at",
        "locked_at",
        "attempts",
    }
    unique = next(
        index
        for index in inspector.get_indexes("usagesession")
        if index["name"] == "uq_usagesession_active_device"
    )
    assert unique["unique"]
    command.check(migration_config(database_url))


def test_fresh_upgrade_matches_models_and_is_repeatable(database_url, engine):
    command.upgrade(migration_config(database_url), "head")
    assert_current_schema(database_url, engine)
    command.upgrade(migration_config(database_url), "head")
    assert_current_schema(database_url, engine)


@pytest.mark.parametrize("compact_uuid", [False, True])
@pytest.mark.parametrize("device_created_at", [False, True])
def test_legacy_adoption_preserves_money_identity_and_dates(
    database_url,
    engine,
    compact_uuid,
    device_created_at,
):
    session_id = SESSION_ID.replace("-", "") if compact_uuid else SESSION_ID
    create_legacy(engine, session_id=session_id, device_created_at=device_created_at)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE django_auth_sentinel (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO django_auth_sentinel VALUES (17)")
    command.upgrade(migration_config(database_url), "head")
    assert_current_schema(database_url, engine)
    metadata = sa.MetaData()
    session = sa.Table("usagesession", metadata, autoload_with=engine)
    device = sa.Table("device", metadata, autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(session)).one()
        assert row.id == session_id
        assert row.total_cost == Decimal("123.4567")
        assert row.is_active is True
        assert row.last_ping_at.replace(tzinfo=UTC) == datetime(2025, 1, 1, tzinfo=UTC)
        assert row.created_at is not None
        device_row = connection.execute(sa.select(device)).one()
        assert device_row.rate_per_unit == Decimal("0.1250")
        if device_created_at:
            assert device_row.created_at.replace(tzinfo=UTC) == datetime(2024, 1, 1, tzinfo=UTC)
        assert connection.exec_driver_sql("SELECT id FROM django_auth_sentinel").scalar_one() == 17
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM usageevent").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM invoiceoutbox").scalar_one() == 0


@pytest.mark.parametrize(
    "corruption,expected",
    [
        ("UPDATE usagesession SET total_cost=-1", "invalid money"),
        ("UPDATE usagesession SET total_cost=100000000", "invalid money"),
        ("UPDATE usagesession SET total_cost=0.00001", "invalid money"),
        ("UPDATE device SET rate_per_unit=-1", "invalid money"),
        ("UPDATE usagesession SET device_id='dev_missing'", "foreign-key violations"),
        ("UPDATE usagesession SET id='invalid-uuid'", "not a UUID"),
        (
            "INSERT INTO usagesession SELECT '1e3190c7-223d-4e87-b37a-d2d94fb29318', "
            "device_id,total_cost,is_active,last_ping_at FROM usagesession",
            "multiple active sessions",
        ),
        ("ALTER TABLE device ADD COLUMN unknown_column TEXT", "unexpected columns"),
        ("CREATE TABLE usageevent (id INTEGER PRIMARY KEY)", "unknown unversioned"),
    ],
)
def test_invalid_legacy_refused_before_application_changes(
    database_url, engine, corruption, expected
):
    create_legacy(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(corruption)
        original_total = connection.exec_driver_sql(
            "SELECT SUM(total_cost) FROM usagesession"
        ).scalar()
        original_count = connection.exec_driver_sql("SELECT COUNT(*) FROM usagesession").scalar()
    with pytest.raises(RuntimeError, match=expected):
        command.upgrade(migration_config(database_url), "head")
    inspector = sa.inspect(engine)
    assert "invoiceoutbox" not in inspector.get_table_names()
    assert "created_at" not in {column["name"] for column in inspector.get_columns("usagesession")}
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT SUM(total_cost) FROM usagesession").scalar()
            == original_total
        )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM usagesession").scalar()
            == original_count
        )


def test_fresh_database_enforces_active_session_and_money_invariants(database_url, engine):
    command.upgrade(migration_config(database_url), "head")
    metadata = sa.MetaData()
    device = sa.Table("device", metadata, autoload_with=engine)
    session = sa.Table("usagesession", metadata, autoload_with=engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            device.insert().values(
                id="dev_migration1",
                rate_per_unit=Decimal("1.0000"),
                is_active=True,
                created_at=now,
            )
        )
        connection.execute(
            session.insert().values(
                id=SESSION_ID,
                device_id="dev_migration1",
                total_cost=Decimal("0"),
                is_active=True,
                created_at=now,
            )
        )
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            session.insert().values(
                id=str(uuid4()),
                device_id="dev_migration1",
                total_cost=Decimal("0"),
                is_active=True,
                created_at=now,
            )
        )
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(device.update().values(rate_per_unit=Decimal("-1")))


def test_destructive_downgrade_refused(database_url, engine):
    command.upgrade(migration_config(database_url), "head")
    with pytest.raises(RuntimeError, match="destroy billing/idempotency"):
        command.downgrade(migration_config(database_url), "base")
    assert_current_schema(database_url, engine)


@pytest.mark.parametrize("legacy", [True, False])
def test_migration_failure_rolls_back_all_application_ddl(
    database_url, engine, monkeypatch, legacy
):
    if legacy:
        create_legacy(engine)
    original_tables = set(sa.inspect(engine).get_table_names())
    create_table = Operations.create_table

    def fail_after_earlier_ddl(self, name, *args, **kwargs):
        if name == "invoiceoutbox":
            raise RuntimeError("injected migration failure")
        return create_table(self, name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_after_earlier_ddl)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        command.upgrade(migration_config(database_url), "head")
    # Alembic may create its own empty version table before running a revision.
    assert set(sa.inspect(engine).get_table_names()) - {"alembic_version"} == original_tables
    if legacy:
        assert "created_at" not in {
            column["name"] for column in sa.inspect(engine).get_columns("usagesession")
        }
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT id FROM usagesession").scalar_one() == SESSION_ID
            )
            assert connection.exec_driver_sql(
                "SELECT total_cost FROM usagesession"
            ).scalar_one() == pytest.approx(123.4567)


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_URL"), reason="requires disposable PostgreSQL"
)
def test_postgres_legacy_native_uuid_adoption():
    """Use an isolated schema, never drop the CI/developer database's public tables."""
    base_url = make_url(os.environ["TEST_POSTGRES_URL"])
    schema = f"migration_test_{uuid4().hex}"
    admin_engine = sa.create_engine(base_url)
    url = base_url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = sa.create_engine(url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        create_legacy(engine, native_uuid=True, device_created_at=True)
        database_url = url.render_as_string(hide_password=False)
        command.upgrade(migration_config(database_url), "head")
        assert_current_schema(database_url, engine)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT id FROM usagesession").scalar_one() == SESSION_ID
            )
            assert connection.exec_driver_sql(
                "SELECT total_cost FROM usagesession"
            ).scalar_one() == Decimal("123.4567")
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin_engine.dispose()
