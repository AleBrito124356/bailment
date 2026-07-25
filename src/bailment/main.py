"""The ASGI application: every surface bailment exposes, assembled in one place.

Four surfaces, one process, and the reason they share a process is that they must share a
catalog. An MCP tool list and an OSB catalog rendered from two different loads of the same
directory can disagree, and the moment they disagree an agent has a capability a human was
never shown. :func:`create_app` loads the catalog exactly once and hands the same
immutable object to all of them.

::

    /v2       Open Service Broker API v2.16 -- humans, CI systems, anything already
              speaking OSB. The one surface that returns credential values, and only to
              an operator token.
    /mcp      Streamable-HTTP MCP -- agents. Never returns a credential value.
    /api/v1   The dashboard's REST API, if this build ships one. It owns that prefix; see
              :func:`_mount_dashboard`.
    /health   Readiness, which checks the database. /health/live for liveness.
    /metrics  Prometheus text format, aggregate counts only.

**What happens when, and why that order.** Logging is configured first, so anything that
fails afterwards fails in the format the operator asked for rather than in whatever
structlog defaults to. Then the catalog, because a broken golden path file has to stop the
process before it binds a port -- a broker serving half a catalog is worse than a broker
that did not start. Then the providers, registered whether or not they are configured, so
that a missing token reads as "cloudflare: not configured" instead of turning up later as
an unknown-provider error. Those three happen in :func:`create_app`, before there is an
event loop.

The lifespan then does the parts that need one: the schema check, and the background
engine. The engine starts last because a worker that starts before the catalog is loaded
has nothing to provision against.

**Who runs the engine.** ``bailment.main:app`` -- the ASGI object a plain ``uvicorn
bailment.main:app`` serves -- runs the lease worker, the ticker and the reconciler in
process, because that deployment has nowhere else to put them. :func:`create_app` itself
defaults to *not* running them, because its callers are compositions that already have
their own: ``bailment serve`` supervises all three, and a test wants a deterministic
process with nothing ticking underneath it. Two workers in one process share
``settings.worker_id``, and while the conditional claim in
:meth:`bailment.engine.worker.ProvisioningWorker._claim` keeps that safe, they poll twice
as often for no benefit and make the log impossible to read.

An application with no engine says so at startup, at warning level. A broker whose leases
are never provisioned and never expire is the failure this project exists to prevent, and
it is not something anybody should have to infer from an absence.

**Schema creation is conditional, deliberately.** ``init_db`` runs for SQLite, which is
the zero-setup demo and the test suite. Against Postgres it is skipped in favour of
Alembic: ``create_all`` against a database with a *stale* schema does nothing while
looking exactly like success, and finding that out at the first provision request is much
worse than finding it out at ``bailment db upgrade``.

**CORS is one origin.** The dashboard's, derived from ``BAILMENT_PUBLIC_BASE_URL``. Not a
wildcard: this broker holds an approval endpoint, and an approval that any origin can
drive out of a logged-in operator's browser is not an approval.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select
from starlette.routing import Route

from bailment import __version__
from bailment.catalog.loader import Catalog, CatalogError, load_catalog
from bailment.config import ENV_PREFIX, Settings, get_settings, unknown_env_vars
from bailment.db import (
    dispose_engine,
    get_sessionmaker,
    healthcheck,
    init_db,
    is_sqlite,
    session_scope,
)
from bailment.engine.leases import LeaseTicker
from bailment.engine.reconciler import Reconciler
from bailment.engine.service import aware
from bailment.engine.worker import ProvisioningWorker
from bailment.logging import configure_from_settings, get_logger
from bailment.mcp.server import build_mcp_server
from bailment.models import Lease, ReconcileRun
from bailment.osb.router import OSB_API_VERSION, build_osb_router
from bailment.providers.registry import ProviderRegistry, default_registry
from bailment.states import LIVE_STATES, LeaseState

__all__ = ["create_app"]

log = get_logger("bailment.main")

#: Where the dashboard's REST router lives. It is a separate component with its own
#: package, and a deployment that ships only the broker is a legitimate thing to build, so
#: a missing dashboard is a warning rather than an ImportError.
#:
#: The router carries its own prefix (``/api/v1``) because the dashboard, the CLI and any
#: third-party client hard-code it. It is therefore included *without* one here: adding a
#: prefix would mount it at ``/api/api/v1`` and every client would 404.
_DASHBOARD_MODULES: Final[tuple[str, ...]] = ("bailment.api", "bailment.api.routes")

#: Prometheus wants this exact content type, version parameter included.
_PROMETHEUS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: How long a background component gets to finish what it is doing before it is cancelled.
#: Not politeness: a worker cancelled in the middle of a provider call loses its chance to
#: record what it just created, and an unrecorded creation is the exact shape of an orphan.
_SHUTDOWN_GRACE: Final = 10.0


# --------------------------------------------------------------------------------------
# The background engine
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Engine:
    """The background components and the switch that stops them.

    Kept on ``app.state`` so ``/health`` can report which of them are still alive and so
    the shutdown path has something concrete to wind down rather than a set of tasks it
    hopes somebody remembered to keep a reference to.
    """

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    async def shutdown(self, *, grace: float = _SHUTDOWN_GRACE) -> None:
        """Ask every component to stop, wait out the grace period, then insist."""
        self.stop.set()
        pending = [task for task in self.tasks.values() if not task.done()]
        if pending:
            _, stubborn = await asyncio.wait(pending, timeout=grace)
            for task in stubborn:
                task.cancel()
            await asyncio.gather(*stubborn, return_exceptions=True)
        for name, task in self.tasks.items():
            if task.cancelled() or not task.done():
                continue
            error = task.exception()
            if error is not None:
                log.error("background component failed", component=name, error=str(error))

    def status(self) -> dict[str, str]:
        return {
            name: ("stopped" if task.done() else "running") for name, task in self.tasks.items()
        }


def _start_engine(
    settings: Settings, catalog: Catalog, registry: ProviderRegistry, *, reconcile: bool
) -> Engine:
    """Start the worker, the ticker and, unless it is switched off, the reconciler."""
    engine = Engine()
    sessionmaker = get_sessionmaker()
    engine.tasks["worker"] = asyncio.create_task(
        ProvisioningWorker(
            sessionmaker, catalog=catalog, registry=registry, settings=settings
        ).run_forever(engine.stop),
        name="bailment-worker",
    )
    engine.tasks["ticker"] = asyncio.create_task(
        LeaseTicker(sessionmaker, catalog=catalog, settings=settings).run_forever(engine.stop),
        name="bailment-ticker",
    )
    if reconcile:
        # Unarmed. The periodic reconciler reports; destroying an orphan needs the global
        # switch and a per-provider allowlist, and this sets neither.
        engine.tasks["reconciler"] = asyncio.create_task(
            Reconciler(sessionmaker, registry=registry, settings=settings).run_forever(engine.stop),
            name="bailment-reconciler",
        )
    return engine


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def _mount_dashboard(application: FastAPI) -> bool:
    """Include the dashboard's REST router, if this build has one.

    A ``ModuleNotFoundError`` naming some *other* module is re-raised rather than
    swallowed. If ``bailment.api`` exists but fails to import because one of its own
    dependencies is missing, reporting that as "no dashboard here" sends the reader
    looking for a file that is sitting right there.

    ``install_error_handlers`` is called when the package offers it, so that a malformed
    request body produces the dashboard's error envelope instead of FastAPI's. It is
    optional by that package's own design; the OSB router is unaffected either way, since
    it parses its own bodies precisely so that it can keep the OSB error envelope.
    """
    for module_name in _DASHBOARD_MODULES:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if missing and (module_name == missing or module_name.startswith(f"{missing}.")):
                continue
            raise
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            continue
        application.include_router(router)
        installer = getattr(module, "install_error_handlers", None)
        if callable(installer):
            installer(application)
        log.info(
            "dashboard API mounted",
            module=module_name,
            prefix=getattr(module, "API_PREFIX", router.prefix),
        )
        return True
    return False


def _cors_origins(settings: Settings) -> list[str]:
    """The one origin the dashboard is served from, as an origin and nothing more."""
    parts = urlsplit(settings.public_base_url)
    if not parts.scheme or not parts.netloc:
        return []
    return [f"{parts.scheme}://{parts.netloc}"]


def _load_catalog(settings: Settings) -> Catalog:
    """Load the catalog, or refuse to build an application at all.

    :class:`~bailment.catalog.loader.CatalogError` already names the file and the field,
    so it is re-raised as itself: wrapping it would bury the one line that says which
    file to open.
    """
    try:
        return load_catalog(settings.catalog_dir)
    except CatalogError:
        log.error(
            "catalog failed to load; refusing to build the application",
            directory=str(settings.catalog_dir),
        )
        raise


def _check_catalog_against_providers(
    catalog: Catalog, registry: ProviderRegistry, settings: Settings
) -> None:
    """Say out loud, once, which golden paths cannot currently be provisioned.

    Not fatal. An installation with a Neon token and no Cloudflare token is an entirely
    normal installation, and refusing to start would make adding a fifth golden path a
    deployment risk. What is not acceptable is silence: a path whose provider is missing
    would otherwise surface as a lease sitting in PENDING with nobody able to say why.
    """
    for path in catalog.enabled():
        if path.provider not in registry:
            log.error(
                "golden path names a provider that is not registered; requests for it will "
                "be parked, never provisioned",
                golden_path=path.id,
                provider=path.provider,
                registered=registry.names(),
            )
            continue
        if not registry.get(path.provider).is_available():
            log.warning(
                "golden path's provider is registered but not configured; requests for it "
                "will wait until its credentials are set",
                golden_path=path.id,
                provider=path.provider,
                missing=list(settings.missing_provider_credentials(path.provider)),
            )


async def _prepare_database(settings: Settings) -> None:
    """Create the schema on SQLite; verify connectivity everywhere else."""
    if is_sqlite(settings.database_url):
        await init_db()
        log.info("sqlite schema ensured")
        return
    if not await healthcheck():
        raise RuntimeError(
            "the database did not answer SELECT 1; bailment will not serve requests it "
            "cannot record"
        )
    log.info(
        "database reachable; the schema is Alembic's to manage",
        remedy="run 'bailment db upgrade' if a table is missing",
    )


def create_app(
    *,
    settings: Settings | None = None,
    catalog: Catalog | None = None,
    registry: ProviderRegistry | None = None,
    run_engine: bool = False,
    run_reconciler: bool | None = None,
) -> FastAPI:
    """Build the application.

    ``settings``, ``catalog`` and ``registry`` are injectable so that a test can stand the
    whole surface up against a temporary directory and an in-memory database without
    touching the process-wide singletons. Left as ``None`` they come from configuration,
    which is what every real deployment does.

    ``run_engine`` starts the lease worker, the ticker and the reconciler in this process.
    It is off by default because every caller of this function is a composition that
    supervises them itself; ``bailment.main:app`` is the one that turns it on. See the
    module docstring. ``run_reconciler`` defaults to ``settings.reconcile_enabled``.
    """
    resolved = settings or get_settings()
    configure_from_settings(resolved)

    loaded = catalog if catalog is not None else _load_catalog(resolved)
    providers = registry or default_registry()
    _check_catalog_against_providers(loaded, providers, resolved)

    mcp_server = build_mcp_server(
        get_sessionmaker(), catalog=loaded, registry=providers, settings=resolved
    )

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        for name in unknown_env_vars():
            log.warning(
                "ignoring an unrecognised environment variable; almost always a typo, and "
                "a misspelled credential shows up later as 'provider unavailable' and "
                "nothing else",
                variable=name,
            )
        if not resolved.auth_configured and not resolved.allow_anonymous:
            log.warning(
                "nothing can authenticate against this broker: no tokens are configured "
                "and anonymous access is off",
                remedy=f"set {ENV_PREFIX}API_TOKENS",
            )

        await _prepare_database(resolved)

        engine = Engine()
        if run_engine:
            engine = _start_engine(
                resolved,
                loaded,
                providers,
                reconcile=(
                    resolved.reconcile_enabled if run_reconciler is None else run_reconciler
                ),
            )
        else:
            log.warning(
                "no lease engine in this process: nothing here provisions a request, "
                "expires a lease or destroys a resource. That is correct if another "
                "process is running them and a serious problem if not",
                remedy="run 'bailment worker', or build the app with run_engine=True",
            )
        application.state.engine = engine

        log.info(
            "bailment ready",
            version=__version__,
            golden_paths=len(loaded),
            catalog=str(loaded.directory),
            providers_available=providers.available_names(),
            components=sorted(engine.tasks),
            osb_api_version=OSB_API_VERSION,
        )
        try:
            # The MCP session manager owns a task group and refuses to serve a request
            # until it is running, so its context has to wrap the serving window rather
            # than be entered per request.
            async with mcp_server.lifespan():
                yield
        finally:
            await engine.shutdown()
            await dispose_engine()
            log.info("bailment stopped")

    application = FastAPI(
        title="bailment",
        version=__version__,
        summary=(
            "A provisioning broker that hands AI agents capabilities instead of "
            "credentials, on leases that destroy themselves."
        ),
        lifespan=lifespan,
    )
    # Read by the OSB router on every request rather than captured in a closure, so that a
    # future catalog reload can swap the object and be picked up without a restart.
    application.state.settings = resolved
    application.state.catalog = loaded
    application.state.registry = providers
    application.state.sessionmaker = get_sessionmaker()
    application.state.engine = Engine()
    application.state.mcp = mcp_server

    origins = _cors_origins(resolved)
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            # X-Broker-API-Version is allow-listed because a browser-based OSB client is a
            # real thing -- every internal developer portal that ships one -- and a custom
            # request header that is not on this list fails the preflight silently.
            allow_headers=["Authorization", "Content-Type", "X-Broker-API-Version"],
            max_age=600,
        )

    application.include_router(build_osb_router(), prefix="/v2")

    if not _mount_dashboard(application):
        log.warning(
            "no dashboard router found, so the REST API is not mounted; the broker itself "
            "is unaffected",
            looked_for=list(_DASHBOARD_MODULES),
        )

    # Two exact routes rather than a Mount. A Starlette Mount at "/mcp" does not match
    # "/mcp" itself -- it matches "/mcp/..." -- so the router answers a redirect, and an
    # MCP client that POSTs its initialize request without following redirects sees a 307
    # and gives up.
    for path, route_name in (("/mcp", "mcp"), ("/mcp/", "mcp_trailing_slash")):
        application.router.routes.append(
            Route(
                path,
                endpoint=mcp_server,
                methods=["GET", "POST", "DELETE"],
                name=route_name,
                include_in_schema=False,
            )
        )

    _register_operational_routes(application)
    return application


# --------------------------------------------------------------------------------------
# Health and metrics
# --------------------------------------------------------------------------------------


def _register_operational_routes(application: FastAPI) -> None:
    @application.get("/health/live", include_in_schema=False)
    async def live() -> Response:
        """Liveness: answers as long as this process can still run a coroutine.

        Separate from ``/health`` on purpose. A readiness probe that fails while the
        database is unreachable is correct; a liveness probe that does the same thing
        restarts every replica during a failover, which is how a database blip becomes an
        outage.
        """
        return JSONResponse({"status": "alive", "version": __version__})

    @application.get("/health", summary="Readiness: can this broker serve?")
    async def health(request: Request) -> Response:
        catalog = getattr(request.app.state, "catalog", None)
        registry = getattr(request.app.state, "registry", None)
        engine = getattr(request.app.state, "engine", None)

        database_ok = False
        detail: str | None = None
        try:
            database_ok = await healthcheck()
        except Exception as exc:
            # The reason belongs in the response: "degraded" with no explanation sends
            # whoever is on call to read logs for something the probe already knows.
            detail = f"{type(exc).__name__}: {exc}"

        payload: dict[str, Any] = {
            "status": "ok" if database_ok else "degraded",
            "version": __version__,
            "osb_api_version": OSB_API_VERSION,
            "database": {"ok": database_ok, "detail": detail},
            "golden_paths": len(catalog) if isinstance(catalog, Catalog) else 0,
            "providers": (
                [status.as_dict() for status in registry.report()]
                if isinstance(registry, ProviderRegistry)
                else []
            ),
            # A component that has stopped is reported, not hidden -- but it does not fail
            # the probe. Taking the API out of the load balancer because a worker died
            # would also remove the only surface that can answer "what happened to my
            # lease", at the moment somebody most needs to ask.
            "components": engine.status() if isinstance(engine, Engine) else {},
        }
        return JSONResponse(payload, status_code=200 if database_ok else 503)

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus exposition, hand-rolled.

        Hand-rolled because ``prometheus_client`` is not in this project's dependency set
        and one endpoint does not justify adding one. Everything here is an aggregate:
        counts by state, cost, and the reconciler's numbers by provider. No lease id, no
        principal, no external name -- a metrics endpoint tends to be the least-guarded
        thing in a deployment, so it carries the least.
        """
        async with session_scope() as session:
            by_state = await session.execute(
                select(Lease.state, func.count()).group_by(Lease.state)
            )
            counts = {str(state): int(count) for state, count in by_state.all()}
            live_leases = await session.execute(
                select(func.count())
                .select_from(Lease)
                .where(Lease.state.in_([s.value for s in LIVE_STATES]))
            )
            live_total = int(live_leases.scalar_one())
            hourly = await session.execute(
                select(func.coalesce(func.sum(Lease.estimated_hourly_usd), 0.0)).where(
                    Lease.state.in_([s.value for s in LIVE_STATES])
                )
            )
            hourly_usd = float(hourly.scalar_one())
            runs = await session.execute(
                select(
                    ReconcileRun.provider,
                    func.max(ReconcileRun.started_at),
                    func.sum(ReconcileRun.orphans_found),
                    func.sum(ReconcileRun.orphans_destroyed),
                    func.sum(ReconcileRun.drift_found),
                ).group_by(ReconcileRun.provider)
            )
            reconciled = runs.all()

        lines: list[str] = []
        lines += _metric(
            "bailment_leases",
            "Leases by lifecycle state.",
            "gauge",
            [({"state": state.value}, counts.get(state.value, 0)) for state in LeaseState],
        )
        lines += _metric(
            "bailment_leases_live",
            "Leases believed to have a real resource behind them, and therefore a bill.",
            "gauge",
            [({}, live_total)],
        )
        lines += _metric(
            "bailment_estimated_hourly_usd",
            "Estimated hourly cost of every live lease, from the catalog's cost model.",
            "gauge",
            [({}, hourly_usd)],
        )
        lines += _metric(
            "bailment_orphans_found_total",
            "Orphaned resources found by the reconciler. The headline number: an "
            "installation whose teardown path works keeps this flat at zero.",
            "counter",
            [({"provider": str(row[0])}, int(row[2] or 0)) for row in reconciled],
        )
        lines += _metric(
            "bailment_orphans_destroyed_total",
            "Orphaned resources the reconciler destroyed.",
            "counter",
            [({"provider": str(row[0])}, int(row[3] or 0)) for row in reconciled],
        )
        lines += _metric(
            "bailment_drift_found_total",
            "Leases believed active whose resource had vanished at the provider.",
            "counter",
            [({"provider": str(row[0])}, int(row[4] or 0)) for row in reconciled],
        )
        lines += _metric(
            "bailment_reconcile_last_run_timestamp_seconds",
            "When the reconciler last swept each provider. Alert on this going stale: a "
            "reconciler that has stopped running reports no orphans at all.",
            "gauge",
            [
                ({"provider": str(row[0])}, aware(row[1]).timestamp())
                for row in reconciled
                if row[1] is not None
            ],
        )
        lines += _metric(
            "bailment_build_info",
            "Build and protocol versions, as a constant 1.",
            "gauge",
            [({"version": __version__, "osb_api_version": OSB_API_VERSION}, 1)],
        )
        return PlainTextResponse("\n".join(lines) + "\n", media_type=_PROMETHEUS_CONTENT_TYPE)


def _metric(
    name: str,
    help_text: str,
    kind: str,
    samples: Sequence[tuple[dict[str, str], float | int]],
) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        rendered = (
            "{" + ",".join(f'{key}="{_escape(val)}"' for key, val in sorted(labels.items())) + "}"
            if labels
            else ""
        )
        lines.append(f"{name}{rendered} {value}")
    return lines


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# --------------------------------------------------------------------------------------
# ASGI entry point
# --------------------------------------------------------------------------------------

_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Build the default application the first time ``bailment.main:app`` is looked up.

    PEP 562, and it earns its keep: ``uvicorn bailment.main:app`` works, while *importing*
    this module still does nothing but define functions. A module-level ``app =
    create_app()`` would read the catalog directory and construct a database engine as a
    side effect of an import, which would make a bad golden path file surface as an
    ImportError in the middle of somebody's test collection.
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _app
    if _app is None:
        # The engine is on here and off in create_app: this object is the whole broker in
        # one process, and there is no other process in that deployment to run the clock.
        _app = create_app(run_engine=True)
    return _app
