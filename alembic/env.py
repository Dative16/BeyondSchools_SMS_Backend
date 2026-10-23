"""Alembic environment.

Migrations connect as the *owning* role, not as the application's role: they
create tables, and the application's role is deliberately not allowed to.
`settings.MIGRATION_DATABASE_URL` is that connection, and it is the only place
in the codebase that uses it.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings

# Importing the package registers every table on Base.metadata. Autogenerate
# compares against this, so a model missing from app/models/__init__.py is a
# model Alembic will happily write a DROP TABLE for.
from app.models import Base  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", str(settings.MIGRATION_DATABASE_URL))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate's hands off the row-level security plumbing.

    Policies, triggers and functions are not in the SQLAlchemy metadata, so
    autogenerate cannot see them — but it can see, and try to drop, the partial
    indexes we declare with postgresql_where. Those are declared on the models,
    so they are left alone here; this hook exists for the one thing that is
    not: the alembic_version table in a database the app also owns.
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    context.configure(
        url=str(settings.MIGRATION_DATABASE_URL),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
