"""The operator's terminal, and the surface that turns a reference back into a credential.

``bailment exec`` is the reason this file exists. Every other surface is arranged so a
credential cannot reach anywhere a model can read it: the API returns a reference, the
dashboard renders output *names*, the MCP tools carry lease ids. A value has to become
usable somewhere, and that somewhere is here -- one process, on a machine a human is
sitting at, writing the plaintext straight into a child process's environment. It never
reaches stdout, a log line, shell history or a response body.

The decryption itself is not done here. :meth:`bailment.engine.service.LeaseService.
resolve_binding` is the only function in bailment that opens an envelope, and it decides
whether to from the caller's kind rather than from the caller's good intentions. So the
security property is not "the CLI is careful"; it is "there is one door, and this file
merely knocks on it". If you are reviewing a change here, the question worth asking is
whether it could put the returned mapping anywhere other than the ``env`` handed to the
child a few lines later.

Three rules shape the rest.

**Mutations go through the service, never through the models.** ``revoke`` and ``renew``
call :class:`~bailment.engine.service.LeaseService`, which owns the commit boundaries and
the state machine. An operator tool that wrote ``Lease.state`` itself would be a second
implementation of the lifecycle, and the day it disagreed with the engine would be the
day somebody used it in an incident. The CLI does read a few bookkeeping columns directly
-- attempts, claims, the retry schedule -- because those are deliberately absent from the
caller-facing view and are exactly what you need when a lease will not move. Those reads
write nothing.

**The CLI never calls a provider.** ``revoke`` records an intention; the worker does the
destroying. Only the engine and the reconciler talk to cloud APIs, which is what keeps
``RELEASED`` a statement about reality rather than about what a laptop believed.

**Commands import their heavy dependencies lazily.** ``bailment keygen`` runs on a machine
with no database, no catalog and no key, and ``bailment catalog validate`` runs in CI
containers with no reason to import a web server. uvicorn and alembic are imported inside
the two commands that need them.

This module is the composition root: the only place that constructs the worker, the
ticker and the reconciler and knows they belong in one process. They are imported
directly, because wiring three objects together is exactly what a composition root is
for. The ASGI application is the single exception -- see :func:`_resolve`.
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import json
import os
import socket
import subprocess
import sys
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from pydantic import ValidationError
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bailment import __version__
from bailment.catalog.loader import Catalog, CatalogError, load_catalog
from bailment.catalog.schema import GoldenPath, parse_duration
from bailment.config import (
    ENV_PREFIX,
    ConfigError,
    Settings,
    get_settings,
    unknown_env_vars,
)
from bailment.db import dispose_engine, get_sessionmaker, init_db, session_scope
from bailment.engine import (
    Caller,
    LeaseService,
    LeaseTicker,
    LeaseView,
    OrphanRecord,
    ProvisioningWorker,
    Reconciler,
    ReconcileSummary,
    ServiceError,
)
from bailment.engine.service import aware
from bailment.logging import configure_logging, get_logger, scrub_text
from bailment.models import AuditEvent, Lease, utcnow
from bailment.providers.base import ProviderError
from bailment.providers.registry import ProviderRegistry, UnknownProvider, default_registry
from bailment.secrets import SecretsError, generate_key
from bailment.states import USABLE_STATES, LeaseState

T = TypeVar("T")

#: Repeated on every command that writes to the audit log. An operator running the CLI
#: from a shared jump host needs to be able to say who they actually are.
ActorOption = Annotated[str | None, typer.Option("--actor", help="Who to record as actor.")]

console = Console()
#: Anything that is not the answer to the question asked goes to stderr: warnings, errors,
#: and the note ``keygen`` prints. That is what makes ``bailment keygen > key.txt`` and
#: ``bailment lease list --json | jq`` work with no special casing.
err_console = Console(stderr=True)

log = get_logger("bailment.cli")


# --------------------------------------------------------------------------------------
# Output primitives
# --------------------------------------------------------------------------------------

#: ORPHANED is the only state rendered on a background colour. It is the state this whole
#: project exists to make visible, and it has to be findable in a screenful of green by
#: somebody who is scrolling rather than reading.
_STATE_STYLE: dict[LeaseState, str] = {
    LeaseState.PENDING: "white",
    LeaseState.AWAITING_APPROVAL: "yellow",
    LeaseState.REJECTED: "dim red",
    LeaseState.PROVISIONING: "cyan",
    LeaseState.ACTIVE: "bold green",
    LeaseState.EXPIRING: "bold yellow",
    LeaseState.EXPIRED: "magenta",
    LeaseState.REVOKED: "magenta",
    LeaseState.DEPROVISIONING: "cyan",
    LeaseState.RELEASED: "dim green",
    LeaseState.FAILED: "bold red",
    LeaseState.ORPHANED: "bold white on red",
    LeaseState.UNKNOWN: "bold yellow",
}


def _state_text(state: LeaseState) -> Text:
    return Text(state.value, style=_STATE_STYLE.get(state, "white"))


def _compact_seconds(seconds: int) -> str:
    """``1d03h``, ``2h04m``, ``9m12s``, ``45s``. Two units is as much as anyone reads."""
    seconds = max(0, seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _countdown(remaining: int | None, state: LeaseState) -> Text:
    """Time left on a lease, coloured by urgency.

    An overdue lease is rendered as overdue rather than as zero. The gap between a
    deadline and the teardown that follows it is the only externally visible measure of
    whether the TTL promise is being kept, and rounding it to zero hides exactly that.
    """
    if remaining is None:
        return Text("-", style="dim")
    if remaining <= 0:
        overdue = _compact_seconds(-remaining)
        if state in USABLE_STATES:
            return Text(f"overdue {overdue}", style="bold white on red")
        return Text(f"-{overdue}", style="dim")
    if remaining < 300:
        style = "bold red"
    elif remaining < 1800:
        style = "yellow"
    else:
        style = "green"
    return Text(_compact_seconds(remaining), style=style)


def _relative(moment: datetime | None) -> str:
    """How long ago, from a stored timestamp.

    Every stored datetime goes through :func:`~bailment.engine.service.aware` first:
    SQLite hands back naive values and subtracting one from an aware ``now`` raises, on
    the demo and in CI but never in the Postgres deployment somebody tested against.
    """
    if moment is None:
        return "-"
    delta = int((utcnow() - aware(moment)).total_seconds())
    if delta < 0:
        return f"in {_compact_seconds(-delta)}"
    return f"{_compact_seconds(delta)} ago"


def _stamp(moment: datetime | None) -> str:
    return "-" if moment is None else aware(moment).isoformat(timespec="seconds")


def _short(identifier: str) -> str:
    return identifier[:8]


def _die(message: str, *, code: int = 1, hint: str | None = None) -> NoReturn:
    """Print an error a human can act on, then stop.

    Messages go through :func:`~bailment.logging.scrub_text` because half of them
    originate in a driver or a provider, and those routinely echo a connection string
    back at you.
    """
    err_console.print(Text("error: ", style="bold red") + Text(scrub_text(message)))
    if hint:
        err_console.print(Text(f"  {scrub_text(hint)}", style="dim"))
    raise typer.Exit(code)


def _warn(message: str) -> None:
    err_console.print(Text("warning: ", style="bold yellow") + Text(scrub_text(message)))


def _print_json(payload: Any) -> None:
    """Emit machine-readable output without Rich's wrapping or syntax highlighting."""
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _default_actor() -> str:
    """The principal recorded in the audit log for a change made from a terminal."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no controlling user is not worth dying over
        user = "unknown"
    return f"cli:{user}@{socket.gethostname()}"


def _settings() -> Settings:
    """Load settings, turning a bad value into a sentence rather than a traceback.

    A mistyped ``BAILMENT_ENCRYPTION_KEY`` should print the validator's explanation of
    what a Fernet key looks like, not eleven frames of pydantic internals above it.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        _die(f"configuration is invalid: {problems}")


def _quiet_logging() -> None:
    """Console logging at warning level, for commands whose output is a table.

    A structlog JSON line through the middle of a Rich table helps nobody. The commands
    that genuinely want the log stream -- ``serve``, ``worker``, ``reconcile`` -- configure
    it from settings instead.
    """
    configure_logging("warning", "console")


def _require_terminal(flag: str) -> None:
    """Refuse a live view when stdout is not a terminal.

    Rich renders a ``Live`` region by repainting it, so against a pipe it emits nothing at
    all until the process ends. Left alone, ``bailment lease list --watch | tee log``
    produces a completely silent program, which reads as a hang. Better to say why.
    """
    if not console.is_terminal:
        _die(
            f"{flag} needs a terminal, and stdout is not one",
            hint=f"drop {flag}, or poll '--json' from your script",
        )


# --------------------------------------------------------------------------------------
# Component construction
# --------------------------------------------------------------------------------------
#
# The engine components are imported and constructed directly: this module is the
# composition root, and wiring three objects is exactly its job. The HTTP application is
# the one thing reached by name, because it is the only component whose module may
# legitimately not be installed -- a deployment that runs workers and nothing else has no
# reason to have FastAPI available, and "no API here" should read as a sentence rather
# than as an ImportError from a command nobody thought was importing anything.


class ComponentUnavailable(RuntimeError):
    """A component this CLI drives could not be found under any of its known names."""


@dataclass(frozen=True, slots=True)
class _Component:
    purpose: str
    expects: str
    candidates: tuple[tuple[str, str], ...]

    def describe(self) -> str:
        return ", ".join(f"{module}.{attr}" for module, attr in self.candidates)


_API_APP = _Component(
    purpose="the HTTP API",
    expects="a FastAPI application, or a zero-argument factory returning one",
    candidates=(
        ("bailment.api.app", "create_app"),
        ("bailment.api", "create_app"),
        ("bailment.api.app", "app"),
        ("bailment.api", "app"),
        # Last, and the one that actually answers today. bailment.main assembles every
        # surface -- OSB, MCP, the dashboard router, health and metrics -- and its
        # create_app() leaves the worker, the ticker and the reconciler switched off,
        # which is exactly right here: `serve` supervises those three itself a few
        # frames below. An earlier candidate, if some deployment adds one, still wins.
        ("bailment.main", "create_app"),
    ),
)


def _resolve(component: _Component) -> Any:
    """Import a component, or explain precisely what was looked for.

    A ``ModuleNotFoundError`` naming some *other* module is re-raised rather than
    swallowed. If ``bailment.api.app`` exists but fails to import because a dependency of
    its own is missing, reporting that as "the HTTP API could not be found" sends the
    reader looking for a file that is sitting right there.
    """
    problems: list[str] = []
    for module_name, attr in component.candidates:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if missing and (module_name == missing or module_name.startswith(f"{missing}.")):
                problems.append(f"{module_name}: not importable")
                continue
            raise
        found = getattr(module, attr, None)
        if found is None:
            problems.append(f"{module_name}: has no attribute {attr!r}")
            continue
        return found
    raise ComponentUnavailable(
        f"could not find {component.purpose}. Looked for {component.describe()}; expected "
        f"{component.expects}. Tried: " + "; ".join(problems)
    )


def _load_api_app() -> Any:
    try:
        target = _resolve(_API_APP)
    except ComponentUnavailable as exc:
        _die(str(exc))
    # A factory is a function; an application instance is an object that happens to be
    # callable, so `callable()` cannot tell them apart and `isfunction` can.
    return target() if inspect.isfunction(target) else target


def _build_worker(catalog: Catalog, settings: Settings) -> ProvisioningWorker:
    return ProvisioningWorker(
        get_sessionmaker(), catalog=catalog, registry=default_registry(), settings=settings
    )


def _build_ticker(catalog: Catalog, settings: Settings) -> LeaseTicker:
    return LeaseTicker(get_sessionmaker(), catalog=catalog, settings=settings)


def _build_reconciler(
    settings: Settings,
    *,
    only: str | None = None,
    destroy_for: Sequence[str] = (),
) -> Reconciler:
    """Construct a reconciler, optionally scoped to one provider and armed to destroy.

    Scoping is done by handing it a registry holding one provider rather than by passing a
    filter, because :meth:`Reconciler.run` walks the registry it was given. A smaller
    registry cannot accidentally sweep something the operator did not name.

    Arming deserves its own paragraph. The reconciler requires two switches -- the global
    ``reconcile_auto_destroy_orphans`` and a per-provider allowlist -- so that no single
    misconfiguration can start deleting things. Run from a terminal, the second switch is
    the operator: ``--destroy``, plus a confirmation that lists the resources by name. So
    the CLI supplies the global switch on a *copy* of the settings. Mutating the
    process-wide object would arm every other reconciler in this process for the rest of
    its life, which is precisely the single-switch failure the design is avoiding.
    """
    registry = default_registry()
    if only is not None:
        scoped = ProviderRegistry()
        scoped.register(registry.get(only))
        registry = scoped
    if destroy_for:
        settings = settings.model_copy(update={"reconcile_auto_destroy_orphans": True})
    return Reconciler(
        get_sessionmaker(),
        registry=registry,
        settings=settings,
        destroy_orphans_for=tuple(destroy_for),
    )


# --------------------------------------------------------------------------------------
# Async plumbing
# --------------------------------------------------------------------------------------


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one async command body, dispose the engine, and translate known failures.

    Disposing matters even in a process that is about to exit: an asyncpg pool collected
    by the interpreter shutting down under it emits a page of unrelated-looking exceptions
    that read like a bug in whatever the user just ran.
    """

    async def _wrapped() -> T:
        try:
            return await coro
        finally:
            await dispose_engine()

    try:
        return asyncio.run(_wrapped())
    except KeyboardInterrupt:
        # 130 is the conventional shell code for SIGINT; wrapper scripts check for it.
        raise typer.Exit(130) from None
    except ConfigError as exc:
        _die(str(exc))
    except SecretsError as exc:
        # DecryptionError arrives here. Its message already names the likely cause -- a
        # rotated key -- which is the entire reason secrets.py raises a typed error.
        _die(str(exc))
    except ServiceError as exc:
        _die(str(exc))
    except ProviderError as exc:
        _die(str(exc))
    except ComponentUnavailable as exc:
        _die(str(exc))
    except SQLAlchemyError as exc:
        message = str(exc)
        lowered = message.lower()
        if "no such table" in lowered or "does not exist" in lowered:
            _die(
                f"the database does not have bailment's tables: {message}",
                hint="run 'bailment db upgrade' first",
            )
        _die(f"database error: {message}")
    except SystemExit as exc:
        # Belt and braces for any component that calls sys.exit() from inside a task. The
        # exit code is kept; the twenty frames of asyncio machinery above it are not,
        # because they bury the one line that said what actually went wrong.
        raise typer.Exit(exc.code if isinstance(exc.code, int) else 1) from None


async def _require_schema() -> None:
    """Fail before binding a port if the schema is not there."""
    async with session_scope() as session:
        await session.execute(select(Lease.id).limit(1))


def _service(session: AsyncSession, *, catalog: Catalog, settings: Settings) -> LeaseService:
    return LeaseService(session, catalog=catalog, registry=default_registry(), settings=settings)


async def _resolve_lease_id(session: AsyncSession, given: str) -> str:
    """Turn a lease id prefix into a full id.

    Every table in this CLI prints eight characters, and a tool that prints an
    abbreviation it then refuses to accept is a tool that makes you copy uuids by hand.
    This reads the id column and nothing else; the lease itself is then loaded through the
    service, which is what applies the visibility rules.
    """
    identifier = given.strip()
    if not identifier:
        _die("no lease id given")

    exact = await session.execute(select(Lease.id).where(Lease.id == identifier))
    if exact.scalar_one_or_none() is not None:
        return identifier

    result = await session.execute(
        select(Lease.id, Lease.state)
        .where(Lease.id.startswith(identifier))
        .order_by(Lease.created_at)
        .limit(5)
    )
    matches = list(result.all())
    if not matches:
        _die(f"no lease with id or prefix {identifier!r}")
    if len(matches) > 1:
        listed = ", ".join(f"{_short(row[0])} ({row[1]})" for row in matches)
        _die(f"lease id prefix {identifier!r} is ambiguous: {listed}")
    return str(matches[0][0])


# --------------------------------------------------------------------------------------
# Lease rendering
# --------------------------------------------------------------------------------------


def _lease_table(views: Sequence[LeaseView], *, title: str | None = None) -> Table:
    table = Table(title=title, header_style="bold", expand=False)
    table.add_column("id", style="bold")
    table.add_column("state")
    table.add_column("golden path")
    table.add_column("provider")
    table.add_column("requester")
    table.add_column("expires in", justify="right")
    table.add_column("created", justify="right", style="dim")
    table.add_column("$/h", justify="right", style="dim")
    for view in views:
        table.add_row(
            _short(view.id),
            _state_text(view.state),
            view.golden_path_id,
            view.provider,
            view.on_behalf_of or view.requester,
            _countdown(view.seconds_remaining, view.state),
            _relative(view.created_at),
            f"{view.estimated_hourly_usd:.4f}" if view.estimated_hourly_usd else "-",
        )
    return table


def _lease_panel(view: LeaseView) -> Panel:
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim")
    facts.add_column()

    facts.add_row("id", view.id)
    facts.add_row("state", _state_text(view.state))
    facts.add_row("golden path", f"{view.golden_path_id} @ {view.provider}")
    facts.add_row("requester", view.requester)
    if view.on_behalf_of:
        facts.add_row("on behalf of", view.on_behalf_of)
    facts.add_row("inputs", json.dumps(view.inputs, sort_keys=True) if view.inputs else "-")
    facts.add_row("created", f"{_stamp(view.created_at)}  ({_relative(view.created_at)})")
    if view.activated_at:
        facts.add_row("activated", f"{_stamp(view.activated_at)}  ({_relative(view.activated_at)})")
    if view.expires_at:
        expiry = Text(f"{_stamp(view.expires_at)}  ")
        expiry.append(_countdown(view.seconds_remaining, view.state))
        facts.add_row("expires", expiry)
    if view.released_at:
        facts.add_row("released", _stamp(view.released_at))
    facts.add_row(
        "ttl",
        f"{_compact_seconds(view.ttl_seconds)} granted, "
        f"{_compact_seconds(view.max_ttl_seconds)} ceiling",
    )
    facts.add_row(
        "renewals",
        f"{view.renewals}/{view.max_renewals}" if view.renewable else "not renewable",
    )
    if view.policy_effect:
        facts.add_row("policy", f"{view.policy_effect}: {view.policy_reason or ''}".strip())
    facts.add_row("external name", view.external_name or "-")
    facts.add_row(
        "cost", f"${view.estimated_hourly_usd:.4f}/h" if view.estimated_hourly_usd else "-"
    )
    if view.failure_reason:
        facts.add_row("failure", Text(scrub_text(view.failure_reason), style="red"))
    return Panel(facts, title="lease", title_align="left")


def _binding_panel(view: LeaseView) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    grid.add_row("reference", view.binding_reference or "-")
    # Names for the sealed values, actual values only for the outputs a golden path
    # declares `secret: false`. The service filtered them; nothing here decrypts.
    grid.add_row("sealed", ", ".join(view.secret_output_names) or "-")
    for name, value in sorted(view.outputs.items()):
        grid.add_row(name, value)
    if view.binding_reference:
        grid.add_row(
            "use",
            Text(f"bailment exec {_short(view.id)} -- <command>", style="bold"),
        )
    return Panel(grid, title="binding", title_align="left")


def _approval_panel(view: LeaseView) -> Panel | None:
    approval = view.approval
    if approval is None:
        return None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    grid.add_row("reason", approval.reason)
    grid.add_row("approvers", ", ".join(approval.allowed_approvers) or "any operator")
    grid.add_row("requested", _relative(approval.requested_at))
    if approval.deadline_at:
        grid.add_row(
            "deadline",
            f"{_stamp(approval.deadline_at)} ({_relative(approval.deadline_at)})",
        )
    if approval.decided_at:
        verdict = "approved" if approval.approved else "declined"
        grid.add_row(
            "decision", f"{verdict} by {approval.decided_by} ({_relative(approval.decided_at)})"
        )
        if approval.decision_note:
            grid.add_row("note", approval.decision_note)
    if approval.url:
        grid.add_row("url", approval.url)
    return Panel(grid, title="approval", title_align="left")


def _operations_panel(lease: Lease) -> Panel:
    """Worker bookkeeping, read straight off the row.

    ``LeaseView`` deliberately carries none of this: no caller of the API has any business
    knowing which worker holds a claim or how many attempts are left. An operator staring
    at a lease that will not move needs exactly that, so the CLI reads the columns. It is
    a read of bookkeeping, not of a credential, and it writes nothing.
    """
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    grid.add_row("previous state", lease.previous_state or "-")
    grid.add_row("updated", _relative(lease.updated_at))
    grid.add_row("attempts", str(lease.attempts))
    grid.add_row("next attempt", _relative(lease.next_attempt_at))
    grid.add_row(
        "claim",
        f"{lease.claimed_by} ({_relative(lease.claimed_at)})" if lease.claimed_by else "unclaimed",
    )
    grid.add_row("provider resource", lease.provider_resource_id or "-")
    if lease.idempotency_key:
        grid.add_row("idempotency key", lease.idempotency_key)
    if lease.agent_session:
        grid.add_row("agent session", lease.agent_session)
    return Panel(grid, title="operations", title_align="left")


def _history_panel(events: Sequence[AuditEvent]) -> Panel | None:
    if not events:
        return None
    table = Table(header_style="bold", expand=False, box=None)
    table.add_column("when", style="dim", justify="right")
    table.add_column("actor")
    table.add_column("action")
    table.add_column("transition")
    table.add_column("detail", style="dim")
    for event in events:
        move = (
            f"{event.from_state or '-'} -> {event.to_state or '-'}"
            if event.from_state or event.to_state
            else ""
        )
        table.add_row(
            _relative(event.at),
            event.actor,
            event.action,
            move,
            scrub_text(json.dumps(event.detail, sort_keys=True, default=str))
            if event.detail
            else "",
        )
    return Panel(table, title="history", title_align="left")


def _lease_detail(
    view: LeaseView, lease: Lease | None = None, events: Sequence[AuditEvent] = ()
) -> RenderableType:
    parts: list[RenderableType] = [_lease_panel(view)]
    if view.binding_reference or view.outputs or view.secret_output_names:
        parts.append(_binding_panel(view))
    approval = _approval_panel(view)
    if approval is not None:
        parts.append(approval)
    if lease is not None:
        parts.append(_operations_panel(lease))
    history = _history_panel(events)
    if history is not None:
        parts.append(history)
    return Group(*parts)


# --------------------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------------------

app = typer.Typer(
    name="bailment",
    no_args_is_help=True,
    add_completion=False,
    help="Provisioning broker. Hands out capabilities instead of credentials.",
)
catalog_app = typer.Typer(
    no_args_is_help=True, help="Inspect and validate the golden path catalog."
)
lease_app = typer.Typer(no_args_is_help=True, help="Inspect and control individual leases.")
db_app = typer.Typer(no_args_is_help=True, help="Database schema management.")
app.add_typer(catalog_app, name="catalog")
app.add_typer(lease_app, name="lease")
app.add_typer(db_app, name="db")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"bailment {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """bailment -- time-boxed leases on real infrastructure, for agents and humans."""
    del version


# --------------------------------------------------------------------------------------
# keygen
# --------------------------------------------------------------------------------------


@app.command()
def keygen() -> None:
    """Print a fresh Fernet key for BAILMENT_ENCRYPTION_KEY."""
    key = generate_key()
    # The key alone on stdout so `bailment keygen > key.txt` and `$(bailment keygen)` do
    # the obvious thing; the instruction goes to stderr, where it cannot end up inside a
    # file that is about to be read back as a key.
    sys.stdout.write(key + "\n")
    err_console.print(
        f"Put this in the environment as {ENV_PREFIX}ENCRYPTION_KEY (or in .env). "
        f"Rotating it later means moving the old value into "
        f"{ENV_PREFIX}PREVIOUS_ENCRYPTION_KEYS, not deleting it.",
        style="dim",
    )


# --------------------------------------------------------------------------------------
# serve / worker
# --------------------------------------------------------------------------------------


def _catalog_or_die(directory: Path | None) -> Catalog:
    target = directory or _settings().catalog_dir
    try:
        return load_catalog(target)
    except CatalogError as exc:
        _die(str(exc))


def _startup_warnings(settings: Settings, *, serving: bool) -> None:
    """Say out loud the two things that silently make an installation useless."""
    stray = unknown_env_vars()
    if stray:
        _warn(
            f"ignoring {len(stray)} unrecognised {ENV_PREFIX}* variable(s): "
            f"{', '.join(stray)}. Almost always a typo, and a misspelled credential shows "
            f"up as 'provider unavailable' and nothing else."
        )
    # Only worth saying in a process that answers requests. A worker authenticates nobody.
    if serving and not settings.auth_configured and not settings.allow_anonymous:
        _warn(
            f"no tokens configured and {ENV_PREFIX}ALLOW_ANONYMOUS is off, so nothing can "
            f"authenticate against this broker. Set {ENV_PREFIX}API_TOKENS."
        )


async def _run_api(server: Any) -> None:
    """Run uvicorn as a task that fails like every other task.

    ``Server.serve`` calls ``sys.exit()`` when it cannot bind a port, and asyncio treats a
    ``SystemExit`` raised inside a task as a request to tear the whole loop down
    immediately -- past :func:`_supervise`, so the worker never gets its shutdown grace and
    the operator gets a traceback instead of uvicorn's own "address already in use" line.
    Converting it into an ordinary exception puts the failure back on the normal path.
    """
    try:
        await server.serve()
    except SystemExit as exc:
        raise RuntimeError(f"uvicorn could not start (exit code {exc.code})") from None


async def _supervise(
    tasks: dict[str, asyncio.Task[Any]],
    *,
    stop: asyncio.Event,
    server: Any = None,
) -> int:
    """Run every task until the first one finishes, then wind the rest down.

    The grace period before cancellation is not politeness. A worker cancelled in the
    middle of a provider call loses its chance to record what it just created, and an
    unrecorded creation is precisely the shape of an orphan. Ten seconds is enough for a
    bounded provider call to return and for the row to be written.
    """
    done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)

    stop.set()
    if server is not None:
        server.should_exit = True
    if pending:
        _, stubborn = await asyncio.wait(pending, timeout=10.0)
        for task in stubborn:
            task.cancel()
        await asyncio.gather(*stubborn, return_exceptions=True)

    code = 0
    names = {task: name for name, task in tasks.items()}
    for task in done:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            log.error(
                "component failed", component=names.get(task, "?"), error=scrub_text(str(error))
            )
            code = 1
    return code


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option("--host", help="Bind address. Defaults to the configured host.")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port. Defaults to the configured port.")
    ] = None,
    worker: Annotated[
        bool,
        typer.Option("--worker/--no-worker", help="Run the lease worker and ticker here too."),
    ] = True,
    reconcile: Annotated[
        bool,
        typer.Option("--reconcile/--no-reconcile", help="Run the periodic reconciler here too."),
    ] = True,
) -> None:
    """Run the API, the lease worker, the ticker and the reconciler in one process.

    One process is the right shape for a single instance and for the demo. Split them with
    --no-worker once there is more than one API replica, and run 'bailment worker'
    alongside.
    """
    settings = _settings()
    configure_logging(settings.log_level, settings.log_format)
    _startup_warnings(settings, serving=True)

    import uvicorn  # here, not at module scope: 'bailment keygen' needs no web server

    catalog = _catalog_or_die(None)
    api = _load_api_app()
    bind_host = host or settings.host
    bind_port = port if port is not None else settings.port

    async def _serve() -> int:
        await _require_schema()
        config = uvicorn.Config(
            api,
            host=bind_host,
            port=bind_port,
            # log_config=None keeps uvicorn from replacing the structlog handlers, which is
            # what routes its access log through the secret redactor.
            log_config=None,
            access_log=True,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        stop = asyncio.Event()
        tasks: dict[str, asyncio.Task[Any]] = {
            "api": asyncio.create_task(_run_api(server), name="api")
        }
        if worker:
            tasks["worker"] = asyncio.create_task(
                _build_worker(catalog, settings).run_forever(stop), name="worker"
            )
            tasks["ticker"] = asyncio.create_task(
                _build_ticker(catalog, settings).run_forever(stop), name="ticker"
            )
        if reconcile and settings.reconcile_enabled:
            # Unarmed on purpose: the periodic reconciler reports. Destroying is armed by
            # settings, which the Reconciler reads for itself, or by an operator running
            # 'bailment reconcile --destroy' and looking at the list first.
            tasks["reconciler"] = asyncio.create_task(
                _build_reconciler(settings).run_forever(stop), name="reconciler"
            )

        log.info(
            "serving",
            url=f"http://{bind_host}:{bind_port}",
            components=sorted(tasks),
            catalog=str(settings.catalog_dir),
            golden_paths=len(catalog),
        )
        return await _supervise(tasks, stop=stop, server=server)

    raise typer.Exit(_run(_serve()))


@app.command()
def worker(
    ticker: Annotated[
        bool, typer.Option("--ticker/--no-ticker", help="Also run the TTL ticker.")
    ] = True,
    reconcile: Annotated[
        bool, typer.Option("--reconcile/--no-reconcile", help="Also run the periodic reconciler.")
    ] = False,
) -> None:
    """Run the lease worker, and by default the ticker, without the API.

    The ticker is on by default because a worker without one drives nothing to expiry: the
    leases get provisioned and then held forever, which is the failure this project exists
    to prevent. Turn it off only when another process is definitely running it.
    """
    settings = _settings()
    configure_logging(settings.log_level, settings.log_format)
    _startup_warnings(settings, serving=False)
    catalog = _catalog_or_die(None)

    async def _work() -> int:
        await _require_schema()
        stop = asyncio.Event()
        tasks: dict[str, asyncio.Task[Any]] = {
            "worker": asyncio.create_task(
                _build_worker(catalog, settings).run_forever(stop), name="worker"
            )
        }
        if ticker:
            tasks["ticker"] = asyncio.create_task(
                _build_ticker(catalog, settings).run_forever(stop), name="ticker"
            )
        if reconcile and settings.reconcile_enabled:
            tasks["reconciler"] = asyncio.create_task(
                _build_reconciler(settings).run_forever(stop), name="reconciler"
            )
        log.info("worker started", worker_id=settings.worker_id, components=sorted(tasks))
        return await _supervise(tasks, stop=stop)

    raise typer.Exit(_run(_work()))


# --------------------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------------------


def _summary_table(summary: ReconcileSummary) -> Table:
    table = Table(header_style="bold", expand=False)
    table.add_column("provider")
    table.add_column("checked")
    table.add_column("resources", justify="right")
    table.add_column("leases", justify="right")
    table.add_column("orphans", justify="right")
    table.add_column("destroyed", justify="right")
    table.add_column("drift", justify="right")
    table.add_column("unknown", justify="right")
    table.add_column("errors", justify="right")
    for result in summary.providers:
        orphans = len(result.orphans)
        drift = len(result.drift)
        unknown = len(result.status_unknown)
        errors = len(result.errors)
        table.add_row(
            result.provider,
            Text("yes", style="green")
            if result.checked
            # A provider that could not be swept is not a clean provider. Saying why, on
            # the row, is what stops "nothing found" being read as "nothing there".
            else Text(result.skipped_reason or "skipped", style="yellow"),
            str(result.resources_seen),
            str(result.leases_checked),
            Text(str(orphans), style="bold red" if orphans else "green"),
            Text(
                str(result.orphans_destroyed),
                style="bold" if result.orphans_destroyed else "dim",
            ),
            Text(str(drift), style="yellow" if drift else "dim"),
            Text(str(unknown), style="yellow" if unknown else "dim"),
            Text(str(errors), style="red" if errors else "dim"),
        )
    return table


def _orphan_table(orphans: Sequence[OrphanRecord], *, destroyed: bool = False) -> Table:
    table = Table(header_style="bold", expand=False)
    table.add_column("provider")
    table.add_column("external name", style="bold")
    table.add_column("provider resource id")
    table.add_column("age", justify="right")
    table.add_column("lease")
    table.add_column("why")
    if destroyed:
        table.add_column("result")
    for orphan in orphans:
        row: list[str | Text] = [
            orphan.provider,
            orphan.external_name,
            orphan.provider_resource_id or "<unknown>",
            _compact_seconds(orphan.age_seconds) if orphan.age_seconds is not None else "unknown",
            f"{_short(orphan.lease_id)} ({orphan.lease_state})" if orphan.lease_id else "none",
            orphan.reason,
        ]
        if destroyed:
            row.append(
                Text("destroyed", style="green")
                if orphan.destroyed
                else Text(scrub_text(orphan.destroy_error or "not destroyed"), style="bold red")
            )
        table.add_row(*row)
    return table


def _drift_lines(summary: ReconcileSummary) -> list[Text]:
    lines: list[Text] = []
    for result in summary.providers:
        for record in result.drift:
            lines.append(
                Text(
                    f"drift: lease {_short(record.lease_id)} ({record.external_name}) "
                    f"{record.from_state} -> {record.to_state}: {record.note}",
                    style="yellow",
                )
            )
    return lines


def _all_orphans(summary: ReconcileSummary) -> list[OrphanRecord]:
    return [orphan for result in summary.providers for orphan in result.orphans]


def _drift_exit_code(summary: ReconcileSummary) -> int:
    """``2`` when the sweep found something wrong with the resources it could see.

    Deliberately not :attr:`ReconcileSummary.clean`, which is also false when a provider
    was skipped. A default installation has three unconfigured providers, so a
    clean-based exit code would be 2 on every run forever and would stop carrying any
    information at all -- and the whole point of a distinct code is that a cron job can
    alert on drift without treating it as a crash. A provider that could not be swept is
    still called out on its own row and in a warning; it simply is not drift.
    """
    problems = summary.orphans_found + summary.drift_found + summary.errors + summary.status_unknown
    return 2 if problems else 0


def _report(summary: ReconcileSummary, *, json_output: bool, destroyed: bool) -> None:
    if json_output:
        _print_json(summary.as_dict())
        return
    console.print(_summary_table(summary))
    orphans = _all_orphans(summary)
    if orphans:
        console.print()
        console.print(_orphan_table(orphans, destroyed=destroyed))
    for line in _drift_lines(summary):
        console.print(line)
    if summary.clean:
        console.print("clean: nothing drifted, and every provider answered.", style="bold green")
    elif summary.providers_skipped:
        _warn(
            "not every provider could be swept, so this run says nothing about "
            f"{', '.join(summary.providers_skipped)}"
        )


@app.command()
def reconcile(
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Only sweep this provider.")
    ] = None,
    destroy: Annotated[
        bool, typer.Option("--destroy", help="Destroy what the sweep finds, after confirmation.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the summary as JSON.")] = False,
) -> None:
    """Compare provider reality against the lease table, in both directions.

    Finds resources that exist at a provider with no live lease (orphans), and leases we
    believe are alive whose resource has vanished underneath us (drift). Reporting is
    always safe. --destroy is not, so it runs a reporting pass first, prints every
    resource it would delete by name, and asks.

    Exit code 2 means the sweep ran and found something wrong, so a cron job can alert on
    drift without treating it as a crash.
    """
    settings = _settings()
    configure_logging(settings.log_level, "console")

    if provider:
        try:
            default_registry().require_available(provider)
        except UnknownProvider as exc:
            _die(str(exc))
        except ProviderError as exc:
            _die(str(exc))

    async def _reconcile() -> int:
        summary = await _build_reconciler(settings, only=provider).run()
        _report(summary, json_output=json_output, destroyed=False)

        orphans = _all_orphans(summary)
        if not destroy:
            if orphans and not json_output:
                console.print(
                    f"\n{len(orphans)} orphaned resource(s). Re-run with --destroy to tear "
                    f"them down.",
                    style="bold red",
                )
            return _drift_exit_code(summary)

        if not orphans:
            console.print("nothing to destroy.", style="green")
            return _drift_exit_code(summary)

        if not yes:
            if not sys.stdin.isatty():
                _die(
                    "refusing to destroy without --yes because stdin is not a terminal",
                    hint="pass --yes if this is deliberate, from CI for instance",
                )
            console.print(
                f"about to destroy the {len(orphans)} resource(s) listed above",
                style="bold red",
            )
            typer.confirm("Destroy them? This cannot be undone.", abort=True)

        # A second full pass rather than a delete loop over the list above. The reconciler
        # re-reads the provider before destroying anything, so a resource that acquired a
        # live lease in the seconds since the first pass is no longer an orphan and is left
        # alone. Deleting straight from the printed list would delete it anyway.
        armed = sorted({orphan.provider for orphan in orphans})
        summary = await _build_reconciler(settings, only=provider, destroy_for=armed).run()
        _report(summary, json_output=json_output, destroyed=True)

        failures = [o for o in _all_orphans(summary) if not o.destroyed]
        if failures:
            _warn(
                f"{len(failures)} orphan(s) survived the destroy pass. They stay visible "
                f"and will be found again by the next sweep."
            )
            return 2
        return 0

    raise typer.Exit(_run(_reconcile()))


# --------------------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------------------


@catalog_app.command("list")
def catalog_list(
    directory: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Catalog directory. Defaults to the configured one."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the catalog as JSON.")] = False,
) -> None:
    """List the golden paths this installation offers."""
    _quiet_logging()
    catalog = _catalog_or_die(directory)
    registry = default_registry()

    if json_output:
        _print_json(
            [
                {
                    "id": path.id,
                    "name": path.name,
                    "description": path.description,
                    "provider": path.provider,
                    "enabled": path.enabled,
                    "tags": path.tags,
                    "mcp_tool": path.mcp_tool_name,
                    "default_ttl": str(path.lease.default_ttl),
                    "max_ttl": str(path.lease.max_ttl),
                    "renewable": path.lease.renewable,
                    "max_renewals": path.lease.max_renewals,
                    "outputs": [output.name for output in path.outputs],
                    "estimated_monthly_usd": path.cost.estimated_monthly_usd,
                    "source": str(catalog.source_of(path.id)),
                }
                for path in catalog.all()
            ]
        )
        return

    table = Table(header_style="bold", expand=False)
    table.add_column("id", style="bold")
    table.add_column("name")
    table.add_column("provider")
    table.add_column("ttl def/max", justify="right")
    table.add_column("renew", justify="right")
    table.add_column("outputs")
    table.add_column("$/mo", justify="right")
    table.add_column("tags", style="dim")
    for path in catalog.all():
        if path.provider not in registry:
            provider_cell = Text(f"{path.provider} (unregistered)", style="bold red")
        elif not registry.get(path.provider).is_available():
            provider_cell = Text(f"{path.provider} (unconfigured)", style="yellow")
        else:
            provider_cell = Text(path.provider)
        style = "" if path.enabled else "dim"
        table.add_row(
            Text(path.id, style=style or "bold"),
            Text(path.name if path.enabled else f"{path.name} (disabled)", style=style),
            provider_cell,
            f"{path.lease.default_ttl} / {path.lease.max_ttl}",
            str(path.lease.max_renewals) if path.lease.renewable else "no",
            ", ".join(output.name for output in path.outputs) or "-",
            f"{path.cost.estimated_monthly_usd:,.2f}" if path.cost.estimated_monthly_usd else "-",
            " ".join(path.tags),
            style=style,
        )
    console.print(table)
    console.print(f"{len(catalog)} golden path(s) from {catalog.directory}", style="dim")


def _validate_path(
    path: GoldenPath,
    source: Path,
    registry: ProviderRegistry,
    settings: Settings,
    tool_names: dict[str, str],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if path.provider not in registry:
        errors.append(
            f"{source}: provider: golden path {path.id!r} names provider {path.provider!r}, "
            f"which is not registered. Known providers: {', '.join(registry.names())}"
        )
    elif not registry.get(path.provider).is_available():
        missing = settings.missing_provider_credentials(path.provider)
        detail = f" (set {', '.join(missing)})" if missing else ""
        warnings.append(
            f"{source}: provider {path.provider!r} is registered but not configured"
            f"{detail}, so requests for {path.id!r} will fail at provisioning time"
        )

    # Ids differing only by hyphen against underscore collapse to one MCP tool name, and
    # an agent-facing collision would only ever be discovered by an agent.
    previous = tool_names.get(path.mcp_tool_name)
    if previous is not None:
        errors.append(
            f"{source}: id: {path.id!r} and {previous!r} both produce the MCP tool name "
            f"{path.mcp_tool_name!r}; one would silently shadow the other"
        )
    tool_names[path.mcp_tool_name] = path.id

    if not path.outputs:
        warnings.append(
            f"{source}: outputs: golden path {path.id!r} declares no outputs, so its leases "
            f"produce no binding for a consumer to use"
        )
    return errors, warnings


@catalog_app.command("validate")
def catalog_validate(
    directory: Annotated[
        Path | None, typer.Argument(help="Catalog directory. Defaults to the configured one.")
    ] = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat warnings as failures. For CI.")
    ] = False,
) -> None:
    """Validate every golden path file. Exits non-zero on any error.

    This is the command to wire into CI. Every failure names the file and the field.
    """
    _quiet_logging()
    settings = _settings()
    target = directory or settings.catalog_dir
    try:
        catalog = load_catalog(target)
    except CatalogError as exc:
        err_console.print(Text("invalid catalog", style="bold red"))
        err_console.print(Text(str(exc)))
        raise typer.Exit(1) from None

    registry = default_registry()
    errors: list[str] = []
    warnings: list[str] = []
    tool_names: dict[str, str] = {}
    for path in catalog.all():
        path_errors, path_warnings = _validate_path(
            path, catalog.source_of(path.id), registry, settings, tool_names
        )
        errors.extend(path_errors)
        warnings.extend(path_warnings)

    for problem in warnings:
        _warn(problem)
    for problem in errors:
        err_console.print(Text("error: ", style="bold red") + Text(problem))

    if errors or (strict and warnings):
        raise typer.Exit(1)
    console.print(
        f"ok: {len(catalog)} golden path(s) in {catalog.directory} are valid", style="bold green"
    )


# --------------------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------------------


@app.command()
def providers(
    json_output: Annotated[bool, typer.Option("--json", help="Emit the report as JSON.")] = False,
) -> None:
    """Show which providers are registered, and which are actually usable.

    Reports the *names* of settings that are missing. It never prints a credential, in
    whole or in part: a truncated token is still a token to whoever is reading over a
    shoulder, and no operational question is answered by a prefix.
    """
    _quiet_logging()
    settings = _settings()
    registry = default_registry()

    try:
        catalog: Catalog | None = load_catalog(settings.catalog_dir)
    except CatalogError as exc:
        catalog = None
        _warn(f"catalog could not be loaded, so the golden path column is empty: {exc}")

    report = registry.report()
    if json_output:
        _print_json(
            [
                {
                    **status.as_dict(),
                    "missing_settings": list(settings.missing_provider_credentials(status.name)),
                    "golden_paths": (
                        [p.id for p in catalog.by_provider(status.name)] if catalog else []
                    ),
                }
                for status in report
            ]
        )
        return

    table = Table(header_style="bold", expand=False)
    table.add_column("provider", style="bold")
    table.add_column("status")
    table.add_column("reconcilable")
    table.add_column("missing settings")
    table.add_column("golden paths", style="dim")
    for status in report:
        missing = settings.missing_provider_credentials(status.name)
        table.add_row(
            status.name,
            Text("available", style="green")
            if status.available
            else Text(status.reason or "not configured", style="yellow"),
            Text("yes", style="green")
            if status.supports_reconciliation
            else Text("no", style="yellow"),
            ", ".join(missing) or "-",
            ", ".join(p.id for p in catalog.by_provider(status.name)) if catalog else "-",
        )
    console.print(table)


# --------------------------------------------------------------------------------------
# lease
# --------------------------------------------------------------------------------------


def _operator(actor: str | None) -> Caller:
    """The identity the CLI acts as, for every command including ``exec``.

    An operator, not a human and not ``CallerKind.CLI``: somebody with shell access to the
    broker already holds the database and the encryption key, so pretending the CLI has a
    lesser authority would be theatre. The principal is recorded as ``cli:user@host``, so
    the audit trail still separates a change made at a terminal from one made with a token
    against the API.

    ``CallerKind.CLI`` is not used, deliberately. It grants ``may_read_secrets`` but not
    ``may_see_everything``, so it can only resolve a binding on a lease whose requester is
    literally this CLI process -- which never happens, because leases are requested by
    agents and by humans at the dashboard. Presenting as ``CLI`` would make ``bailment
    exec`` fail on every lease it was built to serve.
    """
    return Caller.operator(actor or _default_actor())


@lease_app.command("list")
def lease_list(
    state: Annotated[
        list[str] | None, typer.Option("--state", "-s", help="Filter by state. Repeatable.")
    ] = None,
    requester: Annotated[
        str | None, typer.Option("--requester", "-r", help="Filter by principal.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Filter by provider.")
    ] = None,
    golden_path: Annotated[
        str | None, typer.Option("--path", help="Filter by golden path id.")
    ] = None,
    live: Annotated[
        bool, typer.Option("--live", "-l", help="Only leases believed to cost money.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum rows.")] = 50,
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Refresh every second with a live countdown.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the leases as JSON.")] = False,
    actor: ActorOption = None,
) -> None:
    """List leases, newest first."""
    _quiet_logging()
    settings = _settings()
    catalog = _catalog_or_die(None)

    if watch and not json_output:
        _require_terminal("--watch")

    states: list[LeaseState] = []
    for name in state or []:
        try:
            states.append(LeaseState(name))
        except ValueError:
            _die(
                f"unknown lease state {name!r}",
                hint="one of: " + ", ".join(s.value for s in LeaseState),
            )
    caller = _operator(actor)

    async def _list() -> int:
        async def fetch() -> list[LeaseView]:
            async with session_scope() as session:
                service = _service(session, catalog=catalog, settings=settings)
                return await service.list_leases(
                    caller,
                    states=states or None,
                    golden_path_id=golden_path,
                    provider=provider,
                    requester=requester,
                    live_only=live,
                    limit=limit,
                )

        if json_output:
            _print_json([view.as_dict() for view in await fetch()])
            return 0
        if not watch:
            views = await fetch()
            if not views:
                console.print("no leases match.", style="dim")
                return 0
            console.print(_lease_table(views))
            return 0

        with Live(console=console, refresh_per_second=2) as live_view:
            while True:
                # Re-read the rows rather than only recomputing the clock: the interesting
                # thing to watch is a lease crossing into EXPIRING and then disappearing.
                views = await fetch()
                clock = utcnow().strftime("%H:%M:%S")
                live_view.update(
                    _lease_table(views, title=f"leases at {clock} UTC  (ctrl-c to stop)")
                )
                await asyncio.sleep(1.0)

    raise typer.Exit(_run(_list()))


async def _load_detail(
    session: AsyncSession, lease_id: str, caller: Caller, service: LeaseService
) -> tuple[LeaseView, Lease | None, list[AuditEvent]]:
    view = await service.get_lease(lease_id, caller)
    lease = await session.get(Lease, lease_id)
    result = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.lease_id == lease_id)
        .order_by(AuditEvent.at.desc())
        .limit(12)
    )
    events = sorted(result.scalars().all(), key=lambda event: event.at)
    return view, lease, events


@lease_app.command("get")
def lease_get(
    lease_id: Annotated[str, typer.Argument(help="Lease id, or an unambiguous prefix of one.")],
    watch: Annotated[bool, typer.Option("--watch", "-w", help="Refresh every second.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the lease as JSON.")] = False,
    actor: ActorOption = None,
) -> None:
    """Show everything known about one lease, except the secret values."""
    _quiet_logging()
    if watch and not json_output:
        _require_terminal("--watch")
    settings = _settings()
    catalog = _catalog_or_die(None)
    caller = _operator(actor)

    async def _get() -> int:
        async with session_scope() as session:
            resolved = await _resolve_lease_id(session, lease_id)
            service = _service(session, catalog=catalog, settings=settings)
            view, lease, events = await _load_detail(session, resolved, caller, service)
            if json_output:
                _print_json(view.as_dict())
                return 0
            if not watch:
                console.print(_lease_detail(view, lease, events))
                return 0

        with Live(console=console, refresh_per_second=2) as live_view:
            while True:
                async with session_scope() as session:
                    service = _service(session, catalog=catalog, settings=settings)
                    view, lease, events = await _load_detail(session, resolved, caller, service)
                live_view.update(_lease_detail(view, lease, events))
                await asyncio.sleep(1.0)

    raise typer.Exit(_run(_get()))


@lease_app.command("revoke")
def lease_revoke(
    lease_id: Annotated[str, typer.Argument(help="Lease id, or an unambiguous prefix of one.")],
    reason: Annotated[
        str, typer.Option("--reason", help="Recorded in the audit log.")
    ] = "revoked from the CLI",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    actor: ActorOption = None,
) -> None:
    """End a lease early. The worker tears the resource down.

    This records an intention and nothing else. It does not call the provider, because a
    laptop is not allowed to be the thing that decides a cloud resource is gone.
    """
    _quiet_logging()
    settings = _settings()
    catalog = _catalog_or_die(None)
    caller = _operator(actor)

    async def _revoke() -> int:
        async with session_scope() as session:
            resolved = await _resolve_lease_id(session, lease_id)
            service = _service(session, catalog=catalog, settings=settings)
            view = await service.get_lease(resolved, caller)

            if view.state is LeaseState.ORPHANED:
                _die(
                    f"lease {_short(view.id)} is ORPHANED: bailment believes the resource "
                    f"exists and an earlier teardown failed",
                    hint="use 'bailment lease retry-teardown' or 'bailment reconcile --destroy'",
                )

            console.print(_lease_panel(view))
            if not yes:
                if not sys.stdin.isatty():
                    _die("refusing to revoke without --yes because stdin is not a terminal")
                typer.confirm(
                    f"Revoke lease {_short(view.id)} ({view.golden_path_id})?", abort=True
                )

            revoked = await service.revoke(resolved, caller, reason=reason)

        console.print(
            f"lease {_short(revoked.id)} is now {revoked.state.value}; "
            f"the worker will tear it down.",
            style="bold",
        )
        return 0

    raise typer.Exit(_run(_revoke()))


@lease_app.command("renew")
def lease_renew(
    lease_id: Annotated[str, typer.Argument(help="Lease id, or an unambiguous prefix of one.")],
    ttl: Annotated[
        str | None,
        typer.Option("--ttl", help="How much longer, e.g. '2h'. Defaults to the path's default."),
    ] = None,
    actor: ActorOption = None,
) -> None:
    """Extend a live lease, within the ceilings its golden path declares."""
    _quiet_logging()
    settings = _settings()
    catalog = _catalog_or_die(None)
    caller = _operator(actor)

    if ttl is not None:
        # Parsed here as well as in the service so a typo costs one line of output rather
        # than a round trip through a lease lookup.
        try:
            parse_duration(ttl)
        except ValueError as exc:
            _die(str(exc))

    async def _renew() -> int:
        async with session_scope() as session:
            resolved = await _resolve_lease_id(session, lease_id)
            service = _service(session, catalog=catalog, settings=settings)
            outcome = await service.renew(resolved, caller, ttl=ttl)

        view = outcome.lease
        console.print(_lease_panel(view))
        console.print(
            f"lease {_short(view.id)} now expires {_stamp(view.expires_at)} "
            f"({_compact_seconds(view.seconds_remaining or 0)} from now), "
            f"renewal {view.renewals}/{view.max_renewals}",
            style="bold green",
        )
        for notice in outcome.notices:
            _warn(notice)
        return 0

    raise typer.Exit(_run(_renew()))


@lease_app.command("retry-teardown")
def lease_retry_teardown(
    lease_id: Annotated[str, typer.Argument(help="Lease id, or an unambiguous prefix of one.")],
    actor: ActorOption = None,
) -> None:
    """Put an orphaned lease back in the teardown queue.

    Orphans are not retried automatically: they got there by exhausting an attempt budget,
    and a loop that keeps calling a provider that keeps refusing hides the problem instead
    of surfacing it. This is how a human says "try again".
    """
    _quiet_logging()
    settings = _settings()
    catalog = _catalog_or_die(None)
    caller = _operator(actor)

    async def _retry() -> int:
        async with session_scope() as session:
            resolved = await _resolve_lease_id(session, lease_id)
            service = _service(session, catalog=catalog, settings=settings)
            view = await service.retry_teardown(resolved, caller)
        console.print(
            f"lease {_short(view.id)} is queued for teardown again ({view.state.value}).",
            style="bold",
        )
        return 0

    raise typer.Exit(_run(_retry()))


# --------------------------------------------------------------------------------------
# exec
# --------------------------------------------------------------------------------------

_STATE_HINTS: dict[LeaseState, str] = {
    LeaseState.PENDING: "no policy decision has been made yet",
    LeaseState.AWAITING_APPROVAL: "an approver has to decide first",
    LeaseState.REJECTED: "policy denied this request",
    LeaseState.PROVISIONING: "a worker is still creating the resource",
    LeaseState.EXPIRED: "the TTL elapsed and the resource is being destroyed",
    LeaseState.REVOKED: "somebody ended this lease early",
    LeaseState.DEPROVISIONING: "the resource is being destroyed right now",
    LeaseState.RELEASED: "the resource is gone",
    LeaseState.FAILED: "provisioning failed",
    LeaseState.ORPHANED: "the resource may exist, but no live lease covers it",
    LeaseState.UNKNOWN: "bailment cannot currently tell what state the resource is in",
}


def _usable_or_die(view: LeaseView) -> str:
    """Check the lease is usable and return its binding reference.

    The service enforces this too, and enforces it again at the moment of decryption.
    Checking here as well buys a message that names the state and says what it means,
    which is the difference between an operator fixing it and an operator filing a bug.
    """
    if not view.usable:
        _die(
            f"lease {_short(view.id)} is {view.state.value}, which is not a usable state "
            f"({_STATE_HINTS.get(view.state, 'not usable')})",
            hint="usable states are: " + ", ".join(sorted(s.value for s in USABLE_STATES)),
        )
    if view.seconds_remaining is not None and view.seconds_remaining <= 0:
        # The row still says usable but the clock disagrees. Believe the clock: the whole
        # promise of this system is that a lease past its deadline stops working.
        _die(
            f"lease {_short(view.id)} passed its deadline "
            f"{_compact_seconds(-view.seconds_remaining)} ago and the ticker has not "
            f"caught up yet"
        )
    if view.binding_reference is None:
        _die(f"lease {_short(view.id)} has no live binding to inject")
    return view.binding_reference


def _exec_context(view: LeaseView, reference: str) -> dict[str, str]:
    """Non-secret facts about the lease, so a child can reason about its own deadline.

    Deliberately in the ``BAILMENT_`` namespace even though a child that is itself a
    bailment process will list them as unrecognised variables. A second namespace would be
    one nobody would ever guess.
    """
    context = {
        "BAILMENT_LEASE_ID": view.id,
        "BAILMENT_BINDING_REF": reference,
        "BAILMENT_GOLDEN_PATH": view.golden_path_id,
    }
    if view.expires_at is not None:
        context["BAILMENT_LEASE_EXPIRES_AT"] = aware(view.expires_at).isoformat()
    return context


@app.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
def exec_command(
    lease_id: Annotated[str, typer.Argument(help="Lease id, or an unambiguous prefix of one.")],
    command: Annotated[
        list[str], typer.Argument(metavar="-- COMMAND [ARGS]...", help="The command to run.")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show which variables would be set; run nothing.")
    ] = False,
    actor: ActorOption = None,
) -> None:
    """Run a command with a lease's credentials in its environment.

    The values are decrypted in this process and written into the child's environment.
    They are never printed, never passed on a command line, and never written anywhere
    that outlives the child. Options come before the lease id:

        bailment exec --dry-run <lease-id> -- psql -c 'select 1'
    """
    _quiet_logging()
    # Click leaves the '--' in place once ignore_unknown_options is on, and the separator
    # is the documented way to call this, so the program to run would be '--' itself.
    argv = command[1:] if command and command[0] == "--" else list(command)
    if not argv:
        _die("no command given", hint="bailment exec <lease-id> -- <command> [args...]")
    settings = _settings()
    catalog = _catalog_or_die(None)
    caller = _operator(actor)

    async def _prepare() -> tuple[dict[str, str], dict[str, str], str]:
        """Resolve the binding. The audit row is committed before the child ever starts.

        On POSIX this process is about to be replaced by the child, so anything written
        afterwards would simply never happen -- and a credential handed out with no record
        of it is worse than one refused. ``resolve_binding`` commits before returning.
        """
        async with session_scope() as session:
            resolved = await _resolve_lease_id(session, lease_id)
            service = _service(session, catalog=catalog, settings=settings)
            view = await service.get_lease(resolved, caller)
            reference = _usable_or_die(view)
            if dry_run:
                # A dry run must not count as an access, so the envelope stays shut. The
                # names are already public: they are in the lease view.
                names = dict.fromkeys([*view.secret_output_names, *view.outputs], "")
                return names, _exec_context(view, reference), view.id
            values = await service.resolve_binding(reference, caller)
            return values, _exec_context(view, reference), view.id

    payload, context, resolved_id = _run(_prepare())

    overridden = sorted(name for name in payload if name in os.environ)
    if dry_run:
        table = Table(header_style="bold", expand=False)
        table.add_column("variable", style="bold")
        table.add_column("source")
        for name in sorted(payload):
            table.add_row(
                name,
                Text("lease binding (overrides an existing value)", style="yellow")
                if name in overridden
                else Text("lease binding", style="green"),
            )
        for name in sorted(context):
            table.add_row(name, Text("lease context", style="dim"))
        console.print(table)
        console.print(f"would run: {' '.join(argv)}", style="dim")
        console.print(f"lease {_short(resolved_id)}, nothing was decrypted.", style="dim")
        return

    if overridden:
        _warn(f"overriding existing environment variable(s): {', '.join(overridden)}")

    child_env = {**os.environ, **context, **payload}

    try:
        if os.name == "posix":
            # Replace this process. The child inherits the terminal, signals reach it
            # directly, and the plaintext exists in exactly one process image.
            os.execvpe(argv[0], argv, child_env)  # noqa: S606
        # On Windows os.execvpe returns control to the shell while the child keeps
        # running, which loses the exit code and breaks ctrl-c. A subprocess is the honest
        # equivalent there.
        completed = subprocess.run(argv, env=child_env, check=False)  # noqa: S603
    except FileNotFoundError:
        _die(f"command not found: {argv[0]}")
    except PermissionError:
        _die(f"not executable: {argv[0]}")
    raise typer.Exit(completed.returncode)


# The service layer points people at ``bailment run`` in its refusal message, and being
# right about the name in an error a user is already confused by is worth one alias.
app.command(
    "run",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)(exec_command)


# --------------------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------------------


def _find_migrations(explicit: Path | None) -> tuple[Path | None, Path | None]:
    """Locate an ``alembic.ini`` and/or a migrations directory.

    Returns ``(ini, script_location)``; either may be ``None``. Both shapes are supported
    because a repository checkout keeps ``alembic.ini`` at the root while an installed
    wheel ships the migrations inside the package, and ``bailment db upgrade`` has to work
    from both.
    """
    if explicit is not None:
        if explicit.is_file():
            return explicit, None
        if explicit.is_dir() and (explicit / "env.py").exists():
            return None, explicit
        _die(f"{explicit} is neither an alembic.ini nor a directory containing env.py")

    package_dir = Path(__file__).resolve().parent
    seen: set[Path] = set()
    for root in [Path.cwd(), *Path.cwd().parents, package_dir, *package_dir.parents]:
        if root in seen:
            continue
        seen.add(root)
        ini = root / "alembic.ini"
        if ini.is_file():
            return ini, None
        for name in ("migrations", "alembic"):
            candidate = root / name
            if (candidate / "env.py").is_file():
                return None, candidate
    return None, None


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Option("--revision", help="Target revision.")] = "head",
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="alembic.ini, or a directory holding env.py."),
    ] = None,
) -> None:
    """Bring the database schema up to date."""
    _quiet_logging()
    settings = _settings()
    ini, script_location = _find_migrations(config)

    if ini is None and script_location is None:
        if not settings.database_url.startswith("sqlite"):
            _die(
                "no alembic.ini and no migrations directory found, and the configured "
                "database is not SQLite. Refusing to guess at the schema of a real "
                "database with create_all, which does nothing against a stale schema "
                "while looking exactly like success.",
                hint="run this from the repository root, or pass --config",
            )
        # The zero-setup path: a fresh SQLite file for the demo and the test suite.
        _run(init_db())
        console.print(
            f"created the schema directly in {settings.database_url} (no migrations "
            f"found). Fine for SQLite and the demo; a Postgres deployment must use "
            f"Alembic, because create_all silently does nothing when the schema is merely "
            f"out of date.",
            style="yellow",
        )
        return

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(ini)) if ini is not None else AlembicConfig()
    if script_location is not None:
        cfg.set_main_option("script_location", str(script_location))
    # Settings own the database URL; whatever sits in alembic.ini is a development
    # leftover. The doubled '%' is not decoration: alembic reads through ConfigParser,
    # which treats a lone '%' in a password as the start of an interpolation and dies.
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

    alembic_command.upgrade(cfg, revision)
    console.print(f"schema is at {revision}.", style="bold green")


if __name__ == "__main__":  # pragma: no cover - module entry point
    app()
