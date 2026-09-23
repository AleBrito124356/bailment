"""Schema migrations: the one place that knows how bailment's database gets its shape.

Every path that creates or checks the schema goes through here -- ``bailment db upgrade``,
the application's startup, ``bailment serve`` and ``bailment worker`` before they begin --
and every one of them uses the Alembic migrations shipped inside the package
(:data:`MIGRATIONS_DIR`). There is no search. An earlier ``db upgrade`` walked every
ancestor of the working directory looking for any ``alembic.ini`` or ``migrations/env.py``,
and would run an unrelated project's migrations against bailment's database and print
"schema is at head". A migration tool whose target is decided by where you happened to
``cd`` is not one.

**Databases from before migrations existed.** bailment 0.1.0 created its tables with
``create_all`` and left no ``alembic_version``. :func:`upgrade` recognises such a database,
compares its actual schema against what the baseline revision would have built, and stamps
it at that revision instead of failing on ``table leases already exists`` -- or, worse,
stamping blindly and leaving a stale schema looking current. A database that matches no
known revision is refused with the differences listed, because guessing about the shape of
somebody's real data is the one thing a migration must not do.

**Why everything runs on a connection handed to Alembic.** ``env.py`` would otherwise
build its own engine and call ``asyncio.run``, which cannot be done from inside the running
event loop of the application's lifespan or a CLI command. So the caller opens the
transaction, Alembic runs inside it, and the caller commits. See ``env.py``.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from bailment.models import Base

__all__ = [
    "BASELINE_REVISION",
    "MIGRATIONS_DIR",
    "SchemaMismatch",
    "UpgradeReport",
    "alembic_config",
    "current_revision",
    "downgrade",
    "head_revision",
    "render_sql",
    "schema_differences",
    "stamp",
    "upgrade",
]

T = TypeVar("T")

#: The migrations shipped in the wheel. The only place :mod:`bailment.schema` looks.
MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"

#: The revision that recreates exactly what 0.1.0's ``create_all`` built.
BASELINE_REVISION: Final = "0001_baseline"

#: Tables that did not exist at the baseline, with the revision that adds each. Used to
#: tell a 0.1.0 database (stamp at the baseline) from one ``create_all`` built with a newer
#: model (stamp at head).
_ADDED_AFTER_BASELINE: Final[dict[str, str]] = {"memory_resources": "0002_memory_resources"}


class SchemaMismatch(RuntimeError):
    """The database has bailment's tables, no migration history, and a shape we do not know.

    Carries the differences so the message can list them.
    """

    def __init__(self, differences: Sequence[str]) -> None:
        self.differences = list(differences)
        listed = "\n".join(f"  - {line}" for line in self.differences[:20])
        more = (
            f"\n  ... and {len(self.differences) - 20} more" if len(self.differences) > 20 else ""
        )
        super().__init__(
            "this database has bailment tables but no migration history, and its schema "
            "matches no revision bailment knows, so it cannot be safely stamped:\n"
            f"{listed}{more}\n"
            "Inspect it by hand. If it really is at a known revision, record that with "
            "'bailment db stamp <revision>' and upgrade from there."
        )


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """What :func:`upgrade` did, so a caller can say it in a sentence."""

    before: str | None
    after: str | None
    stamped_legacy_at: str | None = None
    """Set when a pre-migration ``create_all`` database was recognised and stamped."""

    @property
    def changed(self) -> bool:
        return self.before != self.after or self.stamped_legacy_at is not None


def alembic_config(database_url: str, *, script_location: Path | None = None) -> Config:
    """An Alembic config for this database, built in code rather than read from a file.

    Settings own the URL. The doubled ``%`` is not decoration: Alembic reads its options
    through ConfigParser, which treats a lone ``%`` in a password as the start of an
    interpolation and dies.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location or MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def head_revision(*, script_location: Path | None = None) -> str:
    head = ScriptDirectory(str(script_location or MIGRATIONS_DIR)).get_current_head()
    if head is None:  # pragma: no cover - the package always ships revisions
        raise RuntimeError(f"no migrations found in {script_location or MIGRATIONS_DIR}")
    return head


def _baseline_metadata() -> MetaData:
    """The metadata as it stood at :data:`BASELINE_REVISION`."""
    baseline = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in _ADDED_AFTER_BASELINE:
            table.to_metadata(baseline)
    return baseline


#: Alembic names each difference by the operation that would make the *database* match the
#: models, which reads backwards to anyone looking at the database. Say what is there.
_VERBS: Final[dict[str, str]] = {"add": "missing", "remove": "unexpected"}


def _describe(diff: Any) -> str:  # noqa: ANN401 - alembic's diff tuples are untyped
    """One readable line per difference, from Alembic's tuples (or lists, for columns)."""
    if isinstance(diff, list):
        return "; ".join(_describe(item) for item in diff)
    if not (isinstance(diff, tuple) and diff):
        return str(diff)
    operation = str(diff[0])
    if operation.startswith("modify_"):
        # (op, schema, table, column, kw, existing, new)
        table, column = diff[2], diff[3]
        return f"column {table}.{column} differs ({operation.removeprefix('modify_')})"
    verb, _, kind = operation.partition("_")
    thing = next((part for part in diff[1:] if hasattr(part, "name")), None)
    table = next((part for part in diff[1:] if isinstance(part, str)), None)
    owner = getattr(getattr(thing, "table", None), "name", None) or table
    name = getattr(thing, "name", None)
    where = ".".join(str(part) for part in (owner, name) if part) if kind == "column" else name
    return f"{_VERBS.get(verb, verb)} {kind} {where or owner or ''}".strip()


def _differences(connection: Connection, metadata: MetaData) -> list[str]:
    """Differences in bailment's own tables. Anything else in the database is not ours.

    A Postgres database is often shared, and a table another application owns is neither
    a reason to refuse a stamp nor something to report.
    """

    def ours(name: str | None, type_: str, parent_names: Any) -> bool:  # noqa: ANN401
        del parent_names
        return type_ != "table" or name in metadata.tables

    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "include_name": ours}
    )
    return [_describe(diff) for diff in compare_metadata(context, metadata)]


def schema_differences(connection: Connection) -> list[str]:
    """How the live schema differs from the models, as readable lines. Empty means none."""
    return _differences(connection, Base.metadata)


def _current(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _upgrade(
    connection: Connection, cfg: Config, revision: str, *, packaged: bool
) -> tuple[str | None, str | None, str | None]:
    before = _current(connection)
    stamped: str | None = None
    # Only the packaged migrations know what 0.1.0's create_all built. Somebody else's
    # migrations get run as they are, and their own env.py decides what that means.
    if packaged and before is None and "leases" in set(inspect(connection).get_table_names()):
        stamped = _legacy_revision(connection)
        cfg.attributes["connection"] = connection
        command.stamp(cfg, stamped)
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, revision)
    return before, _current(connection), stamped


def _legacy_revision(connection: Connection) -> str:
    """Which revision a ``create_all`` database with no history is actually at."""
    if not _differences(connection, Base.metadata):
        return head_revision()
    at_baseline = _differences(connection, _baseline_metadata())
    if not at_baseline:
        return BASELINE_REVISION
    raise SchemaMismatch(at_baseline)


async def _on(engine: AsyncEngine, work: Callable[[Connection], T]) -> T:
    async with engine.begin() as connection:
        return await connection.run_sync(work)


async def upgrade(
    engine: AsyncEngine,
    database_url: str,
    revision: str = "head",
    *,
    script_location: Path | None = None,
) -> UpgradeReport:
    """Bring the schema to ``revision``, stamping a pre-migration database first."""
    cfg = alembic_config(database_url, script_location=script_location)
    packaged = script_location is None or script_location == MIGRATIONS_DIR
    before, after, stamped = await _on(
        engine, lambda conn: _upgrade(conn, cfg, revision, packaged=packaged)
    )
    return UpgradeReport(before=before, after=after, stamped_legacy_at=stamped)


async def downgrade(engine: AsyncEngine, database_url: str, revision: str) -> str | None:
    cfg = alembic_config(database_url)

    def _run(connection: Connection) -> str | None:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, revision)
        return _current(connection)

    return await _on(engine, _run)


async def stamp(engine: AsyncEngine, database_url: str, revision: str) -> str | None:
    """Record ``revision`` as applied without running anything. For operators who know."""
    cfg = alembic_config(database_url)

    def _run(connection: Connection) -> str | None:
        cfg.attributes["connection"] = connection
        command.stamp(cfg, revision)
        return _current(connection)

    return await _on(engine, _run)


async def current_revision(engine: AsyncEngine) -> str | None:
    return await _on(engine, _current)


def render_sql(database_url: str, revision: str = "head", *, start: str | None = None) -> str:
    """The DDL an upgrade would run, as text, touching no database.

    ``start`` renders a range (``0001_baseline:head``), for a DBA reviewing what a new
    release will do to a database already at ``start``.
    """
    buffer = io.StringIO()
    cfg = alembic_config(database_url)
    cfg.output_buffer = buffer
    command.upgrade(cfg, f"{start}:{revision}" if start else revision, sql=True)
    return buffer.getvalue()
