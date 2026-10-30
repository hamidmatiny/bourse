"""Alembic environment. URL from BOURSE_DATABASE_URL via bourse.config; sync psycopg driver."""

from alembic import context
from sqlalchemy import create_engine, pool

from bourse.config import get_settings
from bourse.meter.repo_sql import metadata as meter_metadata
from bourse.registry.repo_sql import metadata as registry_metadata

target_metadata = [registry_metadata, meter_metadata]


def _url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or get_settings().bourse_database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
