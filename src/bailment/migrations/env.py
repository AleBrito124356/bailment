"""Alembic's environment for bailment's own schema.

Shipped inside the package, so an installed wheel migrates its database without a
repository checkout, and located only by :mod:`bailment.schema` -- never by searching the
filesystem. (``bailment db upgrade`` used to walk every ancestor of the working directory
looking for any ``alembic.ini`` or ``migrations/env.py``, and would happily run an unrelated
project's migrations against bailment's database and then report success.)

Three ways in, one migration body:

* **A connection handed over** in ``config.attributes["connection"]``. That is how
  :mod:`bailment.schema` runs migrations from inside an event loop -- the application's
  lifespan, the CLI -- where ``asyncio.run`` is not available. The caller owns the
  transaction.
* **Online, standalone**: a plain ``alembic`` command pointed at this directory. An async
  engine is built from ``sqlalchemy.url`` and driven with ``asyncio.run``.
* **Offline**, ``--sql``: DDL rendered as text for a DBA to read, against no database at
  all. This is also how the Postgres DDL is checked on a machine with no Postgres.

``render_as_batch`` is on for SQLite, whose ``ALTER TABLE`` cannot do most of what a later
migration will want; batch mode rebuilds the table instead. It changes nothing on Postgres.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bailment.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "no sqlalchemy.url in the Alembic config; bailment.schema.alembic_config sets it "
            "from BAILMENT_DATABASE_URL"
        )
    return url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    shared = config.attributes.get("connection")
    if isinstance(shared, Connection):
        do_run_migrations(shared)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
