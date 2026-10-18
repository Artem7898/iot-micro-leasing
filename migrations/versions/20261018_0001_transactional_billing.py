"""Create the billing ledger or safely adopt the recognized pre-ledger schema.

Revision ID: 20261018_0001
Revises:
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from alembic import context, op

revision: str = "20261018_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPLICATION_TABLES = {"device", "usagesession", "usageevent", "invoiceoutbox"}
LEGACY_COLUMNS = {
    "device": {"id", "rate_per_unit", "is_active"},
    "usagesession": {"id", "device_id", "total_cost", "is_active"},
}
OPTIONAL_LEGACY_COLUMNS = {
    "device": {"created_at"},
    "usagesession": {"created_at", "last_ping_at"},
}


def refuse(reason: str) -> None:
    raise RuntimeError(
        f"Cannot adopt legacy billing schema: {reason}. "
        "Back up the database, stop writers, reconcile the reported issue explicitly, "
        "and retry alembic upgrade head. See migrations/README.md; do not stamp head."
    )


def legacy_schema() -> bool:
    """Validate the entire supported legacy layout before changing any application data."""
    if context.is_offline_mode():
        return False  # Offline SQL is intentionally only for a fresh database.
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names()) & APPLICATION_TABLES
    if not existing:
        return False
    if existing != {"device", "usagesession"}:
        refuse("partial, already-modernized, or unknown unversioned application tables")

    for name, required in LEGACY_COLUMNS.items():
        columns = {column["name"]: column for column in inspector.get_columns(name)}
        if (
            not required <= columns.keys()
            or columns.keys() - required - OPTIONAL_LEGACY_COLUMNS[name]
        ):
            refuse(f"unexpected columns in {name}")
        if inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]:
            refuse(f"unexpected primary key in {name}")
        if inspector.get_check_constraints(name):
            refuse(f"custom check constraints in {name} require a reviewed migration")
        if not isinstance(columns["id"]["type"], (sa.String, sa.Uuid)):
            refuse(f"unsupported {name}.id type")
        if name == "device" and not isinstance(columns["id"]["type"], sa.String):
            refuse("device identifiers must already be strings")
        if name == "usagesession" and not isinstance(columns["device_id"]["type"], sa.String):
            refuse("session device identifiers must already be strings")
        money_name = "rate_per_unit" if name == "device" else "total_cost"
        if not isinstance(columns[money_name]["type"], sa.Numeric):
            refuse(f"unsupported {name}.{money_name} type")
        for date_name in {"created_at", "last_ping_at"} & columns.keys():
            if not isinstance(columns[date_name]["type"], sa.DateTime):
                refuse(f"unsupported {name}.{date_name} type")

    # External references can make changing a native UUID key unsafe. A separate
    # reviewed migration must account for their data and type dependencies.
    for name in inspector.get_table_names():
        for foreign_key in inspector.get_foreign_keys(name):
            if foreign_key["referred_table"] in APPLICATION_TABLES and not (
                name == "usagesession"
                and foreign_key["referred_table"] == "device"
                and foreign_key["constrained_columns"] == ["device_id"]
                and foreign_key["referred_columns"] == ["id"]
            ):
                refuse("unexpected foreign-key references to billing tables")

    metadata = sa.MetaData()
    device = sa.Table("device", metadata, autoload_with=connection)
    session = sa.Table("usagesession", metadata, autoload_with=connection)
    for table, money_name, maximum in (
        (device, "rate_per_unit", sa.literal(Decimal("999999.9999"), type_=sa.Numeric(10, 4))),
        (session, "total_cost", sa.literal(Decimal("99999999.9999"), type_=sa.Numeric(12, 4))),
    ):
        money = table.c[money_name]
        invalid = sa.or_(
            money.is_(None),
            money < 0,
            money > maximum,
            money != sa.func.round(money, 4),
            table.c.is_active.is_(None),
            table.c.is_active.not_in([True, False]),
            table.c.id.is_(None),
            sa.func.length(sa.cast(table.c.id, sa.String)) > 36,
        )
        if "created_at" in table.c:
            invalid = sa.or_(invalid, table.c.created_at.is_(None))
        if connection.scalar(sa.select(sa.func.count()).select_from(table).where(invalid)):
            refuse(f"invalid money, identifier, activity flag, or creation date in {table.name}")

    if connection.scalar(
        sa.select(sa.func.count())
        .select_from(session.outerjoin(device, session.c.device_id == device.c.id))
        .where(device.c.id.is_(None))
    ):
        refuse("orphan usage sessions")
    duplicate = connection.execute(
        sa.select(session.c.device_id)
        .where(session.c.is_active.is_(True))
        .group_by(session.c.device_id)
        .having(sa.func.count() > 1)
        .limit(1)
    ).first()
    if duplicate:
        refuse("multiple active sessions for one device; reconcile explicitly without losing debt")
    for (session_id,) in connection.execute(sa.select(session.c.id)).yield_per(1000):
        try:
            UUID(str(session_id))
        except ValueError, TypeError, AttributeError:
            refuse("a session identifier is not a UUID; invoice identity requires manual review")
    return True


def create_core_tables() -> None:
    op.create_table(
        "device",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("rate_per_unit", sa.Numeric(10, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rate_per_unit >= 0 AND rate_per_unit <= 999999.9999",
            name="ck_device_rate_per_unit",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "usagesession",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("total_cost", sa.Numeric(12, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "total_cost >= 0 AND total_cost <= 99999999.9999",
            name="ck_usagesession_total_cost",
        ),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def adopt_legacy_tables() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    captured_at = datetime.now(UTC)
    for name in ("device", "usagesession"):
        columns = {column["name"]: column for column in inspector.get_columns(name)}
        if "created_at" not in columns:
            op.add_column(name, sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
            table = sa.table(name, sa.column("created_at", sa.DateTime(timezone=True)))
            op.execute(table.update().values(created_at=captured_at))
        if name == "usagesession" and "last_ping_at" not in columns:
            op.add_column(
                name, sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True)
            )
        money_name = "rate_per_unit" if name == "device" else "total_cost"
        precision = 10 if name == "device" else 12
        with op.batch_alter_table(name) as batch:
            batch.alter_column("created_at", type_=sa.DateTime(timezone=True), nullable=False)
            batch.alter_column(
                "id",
                type_=sa.String(36),
                existing_type=columns["id"]["type"],
                existing_nullable=False,
                postgresql_using="id::text",
            )
            batch.alter_column(money_name, type_=sa.Numeric(precision, 4), nullable=False)
            if name == "device":
                batch.create_check_constraint(
                    "ck_device_rate_per_unit", "rate_per_unit >= 0 AND rate_per_unit <= 999999.9999"
                )
            else:
                batch.alter_column("last_ping_at", type_=sa.DateTime(timezone=True))
                batch.create_check_constraint(
                    "ck_usagesession_total_cost", "total_cost >= 0 AND total_cost <= 99999999.9999"
                )


def create_ledger_tables() -> None:
    op.create_index(
        "uq_usagesession_active_device",
        "usagesession",
        ["device_id"],
        unique=True,
        sqlite_where=sa.text("is_active = true"),
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index("ix_usagesession_device_created", "usagesession", ["device_id", "created_at"])
    op.create_table(
        "usageevent",
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("total_cost", sa.Numeric(12, 4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["usagesession.id"]),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index("ix_usageevent_device_id", "usageevent", ["device_id"])
    op.create_table(
        "invoiceoutbox",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("total_cost", sa.Numeric(12, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["usagesession.id"]),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_invoiceoutbox_session"),
    )
    op.create_index(
        "ix_invoiceoutbox_delivery",
        "invoiceoutbox",
        ["completed_at", "published_at", "locked_at", "created_at"],
    )


def upgrade() -> None:
    if legacy_schema():
        adopt_legacy_tables()
        for index in sa.inspect(op.get_bind()).get_indexes("usagesession"):
            if (
                index["name"] == "usagesession_device_id_7aae96ad"
                and index["column_names"] == ["device_id"]
                and not index["unique"]
            ):
                # The new full (device_id, created_at) index covers this old
                # Django-generated FK index's leading-column access path.
                op.drop_index(index["name"], table_name="usagesession")
    else:
        create_core_tables()
    create_ledger_tables()


def downgrade() -> None:
    raise RuntimeError(
        "Automatic downgrade would destroy billing/idempotency records. "
        "Restore a tested backup or prepare a separately reviewed data-preserving migration."
    )
