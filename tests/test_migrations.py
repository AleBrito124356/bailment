"""Alembic migrations, shipped in the package, and ``bailment db`` on top of them.

Three promises are tested here. The migrations build exactly the schema the models
describe, in both directions. A database created before migrations existed is recognised
and stamped rather than rebuilt or refused -- and one that matches nothing is refused with
the differences named. And ``bailment db upgrade`` runs bailment's migrations and nobody
else's, wherever it is run from.

There is no Postgres on the machines this suite runs on, so the Postgres DDL is checked the
way a DBA reviews it: rendered offline with ``--sql`` for the ``postgresql+asyncpg``
dialect, which compiles every statement without connecting to anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import AsyncIterator, Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from bailment import db as db_module
from bailment import schema
from bailment.catalog.loader import Catalog
from bailment.cli import app
from bailment.config import Settings, reset_settings_cache
from bailment.db import build_engine, dispose_engine
from bailment.main import create_app
from bailment.models import Base
from bailment.providers import memory as memory_module
from bailment.providers import registry as registry_module
from bailment.providers.registry import ProviderRegistry

HEAD = "0002_memory_resources"
POSTGRES = "postgresql+asyncpg://bailment:secret@db.invalid:5432/bailment"


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[tuple[AsyncEngine, str]]:
    url = sqlite_url(tmp_path / "migrate.db")
    engine = create_async_engine(url)
    yield engine, url
    await engine.dispose()


async def tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))


async def differences(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(schema.schema_differences)


# --------------------------------------------------------------------------------------
# The migrations themselves
# --------------------------------------------------------------------------------------


def test_the_migrations_ship_inside_the_package() -> None:
    """An installed wheel has to be able to migrate with no repository checkout around it."""
    packaged = files("bailment") / "migrations"
    assert (packaged / "env.py").is_file()
    assert (packaged / "script.py.mako").is_file()
    assert schema.MIGRATIONS_DIR.name == "migrations"
    assert schema.head_revision() == HEAD


async def test_upgrade_builds_exactly_what_the_models_describe(
    database: tuple[AsyncEngine, str],
) -> None:
    engine, url = database
    report = await schema.upgrade(engine, url)

    assert report.before is None
    assert report.after == HEAD
    assert report.stamped_legacy_at is None
    assert await differences(engine) == []
    assert await schema.current_revision(engine) == HEAD
    assert {"leases", "bindings", "approvals", "audit_events", "reconcile_runs"} <= await tables(
        engine
    )
    assert "memory_resources" in await tables(engine)


async def test_downgrade_to_base_drops_everything_and_upgrade_rebuilds_it(
    database: tuple[AsyncEngine, str],
) -> None:
    engine, url = database
    await schema.upgrade(engine, url)

    assert await schema.downgrade(engine, url, "0001_baseline") == "0001_baseline"
    assert "memory_resources" not in await tables(engine)
    assert await schema.downgrade(engine, url, "base") is None
    assert await tables(engine) == {"alembic_version"}

    again = await schema.upgrade(engine, url)
    assert again.after == HEAD
    assert await differences(engine) == []


async def test_upgrading_twice_changes_nothing(database: tuple[AsyncEngine, str]) -> None:
    engine, url = database
    await schema.upgrade(engine, url)
    second = await schema.upgrade(engine, url)
    assert second.before == second.after == HEAD
    assert second.changed is False


# --------------------------------------------------------------------------------------
# Databases from before migrations existed
# --------------------------------------------------------------------------------------


async def test_a_0_1_0_create_all_database_is_stamped_at_the_baseline_then_upgraded(
    database: tuple[AsyncEngine, str],
) -> None:
    """The shape every existing install has: the five original tables, rows in them, and
    no alembic_version. It must not be rebuilt, and its rows must survive."""
    engine, url = database
    async with engine.begin() as connection:
        await connection.run_sync(schema._baseline_metadata().create_all)
        await connection.execute(
            text(
                "insert into leases (id, golden_path_id, provider, inputs, requester, state, "
                "created_at, updated_at, ttl_seconds, renewals, provider_ref, "
                "estimated_hourly_usd, attempts) values ('lease-from-0-1-0', 'sandbox', "
                "'memory', '{}', 'agent', 'active', '2026-07-25 18:52:49', "
                "'2026-07-25 18:52:49', 300, 0, '{}', 0.0, 0)"
            )
        )

    report = await schema.upgrade(engine, url)

    assert report.stamped_legacy_at == schema.BASELINE_REVISION
    assert report.after == HEAD
    assert await differences(engine) == []
    async with engine.connect() as connection:
        survived = await connection.execute(text("select id from leases"))
        assert [row[0] for row in survived] == ["lease-from-0-1-0"]


async def test_a_create_all_database_from_current_models_is_stamped_at_head(
    database: tuple[AsyncEngine, str],
) -> None:
    engine, url = database
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    report = await schema.upgrade(engine, url)

    assert report.stamped_legacy_at == HEAD
    assert report.before is None
    assert report.after == HEAD


async def test_a_schema_that_matches_no_revision_is_refused_with_the_differences_named(
    database: tuple[AsyncEngine, str],
) -> None:
    """Stamping it anyway would leave a stale schema looking current, which is the exact
    failure create_all was replaced to prevent."""
    engine, url = database
    async with engine.begin() as connection:
        await connection.run_sync(schema._baseline_metadata().create_all)
        await connection.execute(text("alter table leases add column surprise integer"))
        await connection.execute(text("drop index ix_leases_state"))

    with pytest.raises(schema.SchemaMismatch) as caught:
        await schema.upgrade(engine, url)

    assert "unexpected column leases.surprise" in caught.value.differences
    assert "missing index ix_leases_state" in caught.value.differences
    assert "bailment db stamp" in str(caught.value)
    assert await schema.current_revision(engine) is None  # nothing was recorded


async def test_tables_that_belong_to_somebody_else_are_ignored(
    database: tuple[AsyncEngine, str],
) -> None:
    """A shared Postgres database has other applications' tables in it. Not ours, not a
    reason to refuse, not something to report."""
    engine, url = database
    async with engine.begin() as connection:
        await connection.run_sync(schema._baseline_metadata().create_all)
        await connection.execute(text("create table invoices (id integer primary key)"))

    report = await schema.upgrade(engine, url)
    assert report.stamped_legacy_at == schema.BASELINE_REVISION
    assert "invoices" in await tables(engine)


# --------------------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------------------


async def test_the_application_migrates_a_sqlite_file_when_it_starts(
    settings: Settings,
    catalog: Catalog,
    registry: ProviderRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zero-setup demo: a SQLite file with nothing in it becomes a current schema by
    starting the broker, with the same migrations ``db upgrade`` runs -- not create_all."""
    scoped = settings.model_copy(update={"database_url": sqlite_url(tmp_path / "app.db")})
    engine = build_engine(scoped)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(
        db_module, "_sessionmaker", async_sessionmaker(bind=engine, expire_on_commit=False)
    )
    application = create_app(settings=scoped, catalog=catalog, registry=registry)

    async with application.router.lifespan_context(application):
        pass

    assert await schema.current_revision(engine) == HEAD
    assert await differences(engine) == []
    await engine.dispose()


# --------------------------------------------------------------------------------------
# Postgres, offline
# --------------------------------------------------------------------------------------


def test_the_postgres_ddl_compiles_offline() -> None:
    sql = schema.render_sql(POSTGRES)

    for table in (
        "leases",
        "bindings",
        "approvals",
        "audit_events",
        "reconcile_runs",
        "memory_resources",
    ):
        assert f"CREATE TABLE {table} (" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "CONSTRAINT uq_lease_idempotency UNIQUE (requester, idempotency_key)" in sql
    assert "FOREIGN KEY(lease_id) REFERENCES leases (id) ON DELETE CASCADE" in sql
    assert "BYTEA" in sql
    assert "UPDATE alembic_version SET version_num='0002_memory_resources'" in sql
    assert sql.strip().endswith("COMMIT;")


def test_an_upgrade_between_releases_renders_only_what_changed() -> None:
    """What a DBA reviews before letting a new release at a database already at 0001."""
    sql = schema.render_sql(POSTGRES, start=schema.BASELINE_REVISION)
    assert "CREATE TABLE memory_resources (" in sql
    assert "CREATE TABLE leases (" not in sql


# --------------------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------------------


@pytest.fixture
def shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A configured environment, as ``bailment db`` would find it in a terminal."""
    database = tmp_path / "shell.db"
    monkeypatch.setenv("BAILMENT_DATABASE_URL", sqlite_url(database))
    monkeypatch.setenv("BAILMENT_LOG_LEVEL", "warning")
    reset_settings_cache()
    monkeypatch.setattr(registry_module, "_DEFAULT", None)
    monkeypatch.setattr(memory_module, "_DEFAULT", None)
    yield database
    asyncio.run(dispose_engine())


def sqlite_tables(database: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("select name from sqlite_master where type = 'table'")
        return {row[0] for row in rows}


def test_db_upgrade_never_runs_another_project_s_migrations(
    shell: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's reproduction. ``db upgrade`` used to search the working directory and
    every ancestor for any migrations/env.py, found this one, ran it against bailment's
    database, and printed "schema is at head" with not one bailment table created."""
    project = tmp_path / "otherproject"
    marker = tmp_path / "foreign-env-ran"
    (project / "migrations").mkdir(parents=True)
    (project / "migrations" / "env.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8"
    )
    work = project / "deep" / "work"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)

    result = CliRunner().invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    assert not marker.exists(), "an unrelated project's env.py ran against bailment's database"
    assert {"leases", "bindings", "memory_resources", "alembic_version"} <= sqlite_tables(shell)
    assert f"-> {HEAD}" in result.stdout


def test_db_upgrade_reports_a_stamped_legacy_database(shell: Path) -> None:
    async def legacy() -> None:
        engine = create_async_engine(sqlite_url(shell))
        async with engine.begin() as connection:
            await connection.run_sync(schema._baseline_metadata().create_all)
        await engine.dispose()

    asyncio.run(legacy())
    result = CliRunner().invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "stamped there rather than rebuilt" in result.stdout
    assert "0001_baseline" in result.stdout


def test_db_current_and_stamp(shell: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    current = runner.invoke(app, ["db", "current", "--check"])
    assert current.exit_code == 0
    assert f"{HEAD} (head)" in current.stdout

    stamped = runner.invoke(app, ["db", "stamp", "0001_baseline"])
    assert stamped.exit_code == 0
    assert "no table was changed" in stamped.stdout

    behind = runner.invoke(app, ["db", "current", "--check"])
    assert behind.exit_code == 1
    assert f"head is {HEAD}" in behind.stdout


def test_db_upgrade_sql_prints_ddl_without_touching_a_database(
    shell: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAILMENT_DATABASE_URL", POSTGRES)
    reset_settings_cache()
    result = CliRunner().invoke(app, ["db", "upgrade", "--sql"])
    assert result.exit_code == 0, result.output
    assert "CREATE TABLE memory_resources (" in result.stdout
    assert not shell.exists()
