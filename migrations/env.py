"""Run the API-owned schema migrations using the configured application database."""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from api.core.config import settings
from api.dependencies.db import create_database_engine
from api.models import device  # noqa: F401 -- register target metadata

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def include_object(_object, name, object_type, _reflected, _compare_to) -> bool:
    # Auth/admin tables are managed by Django, not missing SQLModel models.
    return object_type != "table" or name in SQLModel.metadata.tables


def run_migrations() -> None:
    # Programmatic callers can select an isolated DB without changing global settings.
    database_url = config.attributes.get("database_url", settings.DATABASE_URL)
    if context.is_offline_mode():
        context.configure(
            url=database_url,
            target_metadata=SQLModel.metadata,
            include_object=include_object,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            sqlite = connection.dialect.name == "sqlite"
            if sqlite:
                # SQLite requires FK checks disabled before batch table replacement.
                # Check both before and after; all DDL/data copies share a real transaction.
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise RuntimeError("Existing foreign-key violations must be reconciled first")
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
            context.configure(
                connection=connection,
                target_metadata=SQLModel.metadata,
                include_object=include_object,
                compare_type=True,
                render_as_batch=sqlite,
                transactional_ddl=True,
            )
            try:
                with context.begin_transaction():
                    if sqlite:
                        connection.exec_driver_sql("BEGIN")
                    elif connection.dialect.name == "postgresql":
                        connection.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
                    context.run_migrations()
                    if sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                        raise RuntimeError(
                            "Migration produced a foreign-key violation; rolled back"
                        )
            finally:
                if sqlite:
                    if connection.in_transaction():
                        connection.rollback()
                    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    finally:
        engine.dispose()


run_migrations()
