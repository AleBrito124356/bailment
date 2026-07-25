"""Async engine, sessions and schema creation.

The engine is created lazily and held in a module global. Lazily, because importing
:mod:`bailment.db` must not open a connection -- ``bailment keygen`` imports half the
package and has no database -- and globally, because the lease workers and the API in one
process should share a pool rather than each build their own.

**Two session helpers, on purpose.**

:func:`session_scope` is a unit of work: it commits when the block exits cleanly and
rolls back when it does not. That is what a worker wants -- claim, call the provider,
record the result, commit, and if anything raises then none of it happened.

:func:`get_session` is the FastAPI dependency and it never commits. A handler that
returns 200 having deliberately decided not to write must not have its work committed
behind its back on the way out, and a handler that writes then raises after the response
has been decided is a bug we would rather surface than paper over. Endpoints commit
explicitly.

**SQLite needs three pragmas and none of them are optional.** ``foreign_keys=ON`` because
SQLite ignores ``ON DELETE CASCADE`` without it, which would leave bindings behind when a
lease is deleted -- encrypted credentials outliving the thing they belonged to is the
precise failure this project is about. ``journal_mode=WAL`` because the lease engine
writes while the dashboard reads and rollback journaling makes them block each other.
``busy_timeout`` because without it a concurrent writer gets an instant
``database is locked`` instead of waiting the 50ms it needed to.

Schema creation via :func:`init_db` is for SQLite, tests and the zero-setup demo. Anything
with a Postgres behind it uses Alembic; ``create_all`` against a database that already has
tables silently does nothing, which is fine, and against one with a *stale* schema does
nothing while looking like success, which is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bailment.config import Settings, get_settings
from bailment.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_memory_sqlite(url: str) -> bool:
    return is_sqlite(url) and (":memory:" in url or "mode=memory" in url)


def _install_sqlite_pragmas(engine: AsyncEngine, *, wal: bool) -> None:
    """Apply the pragmas SQLite needs on every new connection.

    Per connection, not per engine: SQLite scopes ``foreign_keys`` to the connection, so
    a pool that opens a second one silently loses cascade enforcement on it.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ANN401
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if wal:
                # Meaningless for an in-memory database and noisy if attempted there.
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create an engine for these settings without touching the module global.

    Separate from :func:`get_engine` so tests can stand one up per test without fighting
    the process-wide instance.
    """
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}

    if _is_memory_sqlite(url):
        # An in-memory database lives inside its connection. Without StaticPool every
        # session gets a different, empty database and the tests are a hall of mirrors.
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    elif is_sqlite(url):
        kwargs["connect_args"] = {"timeout": 30}
    else:
        # pool_pre_ping because a broker sits idle between ticks and a stale connection
        # to a database that restarted overnight should cost one retry, not one incident.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        kwargs["pool_recycle"] = 1800
        if "asyncpg" in url:
            # Named connections make "who is holding this lock" answerable from pg_stat.
            kwargs["connect_args"] = {
                "server_settings": {"application_name": f"bailment/{settings.worker_id}"}
            }

    engine = create_async_engine(url, **kwargs)
    if is_sqlite(url):
        _install_sqlite_pragmas(engine, wal=not _is_memory_sqlite(url))
    return engine


def get_engine() -> AsyncEngine:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory.

    ``expire_on_commit=False`` because a worker that commits a lease transition and then
    reads ``lease.state`` to log it should not trigger a lazy refresh -- in async
    SQLAlchemy that refresh raises ``MissingGreenlet`` from whatever line touched the
    attribute, which is never the line that caused it.
    """
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A unit of work: commit on clean exit, roll back on anything else."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Rolls back on error, and deliberately does not commit."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Create any missing tables. Idempotent; see the module docstring for the caveat."""
    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db(engine: AsyncEngine | None = None) -> None:
    """Drop every table bailment owns. For tests and for ``bailment reset``."""
    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    """Close the pool and forget the globals, so the next call builds a fresh engine.

    Called on shutdown, and by tests between cases. Not disposing on shutdown leaves
    asyncpg connections in a state where the event loop closing under them logs a page of
    unrelated-looking exceptions.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def healthcheck(engine: AsyncEngine | None = None) -> bool:
    """Whether the database answers. Used by ``/healthz`` and by the worker's first tick."""
    target = engine or get_engine()
    async with target.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return bool(result.scalar_one() == 1)
