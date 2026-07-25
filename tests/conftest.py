"""Fixtures for the whole suite.

Three constraints shape everything in this file, and they are the reason the suite runs
in a couple of seconds on a laptop with no cloud accounts:

**Zero credentials, zero network.** Every test uses :class:`MemoryProvider`, which
implements the full provider contract in a dict and can be told to fail on demand. No
test needs a Neon token, and the two directions of drift the reconciler exists to find
are producible on purpose rather than by killing a worker at the right microsecond.

**A database per test.** SQLite in memory with ``StaticPool``, built through
:func:`bailment.db.build_engine` so the tests exercise the same pragma installation and
the same pool choice production does. The module-level engine and session factory in
:mod:`bailment.db` are pointed at it too, because :func:`bailment.main.create_app` and the
OSB router reach for the process-wide factory and a test that left those alone would be
writing to whatever ``bailment.db`` a previous run happened to leave on disk.

**Catalogs are built, not parsed.** Most tests want a golden path with one specific policy
rule, and round-tripping that through YAML would make every behavioural test also a test
of the loader. :func:`make_path` builds :class:`GoldenPath` objects directly and
:func:`make_catalog` wraps them in the same immutable :class:`Catalog` the loader returns.
``tests/test_catalog.py`` is where real files on a real disk get parsed.

One fixture is autouse and worth knowing about: ``_isolated_environment`` strips every
``BAILMENT_*`` variable and moves the working directory to a temporary one. Settings read
a ``.env`` from the working directory, so without the chdir a developer with a real
``.env`` in the repo root would get a suite that passes for them and fails in CI, or worse
one that quietly points at their own database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bailment import db as db_module
from bailment.catalog.loader import Catalog
from bailment.catalog.schema import (
    BindingOutput,
    CostModel,
    Duration,
    GoldenPath,
    LeasePolicy,
    PolicyRule,
)
from bailment.config import Settings, reset_settings_cache
from bailment.db import build_engine
from bailment.engine.leases import LeaseNotice, LeaseTicker
from bailment.engine.reconciler import Reconciler
from bailment.engine.service import Caller, LeaseService, ProvisionOutcome, ProvisionRequest
from bailment.engine.worker import ProvisioningWorker
from bailment.logging import reset_logging
from bailment.models import AuditEvent, Base, Lease
from bailment.providers.memory import MemoryProvider
from bailment.providers.registry import ProviderRegistry
from bailment.secrets import SecretBox, generate_key

# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------


class RecordingProvider(MemoryProvider):
    """A memory provider that also remembers the order it was called in.

    :attr:`MemoryProvider.calls` counts operations, which answers "did the retry
    double-provision" but not "did the worker drain teardown before provisioning". The
    ordered log answers both, and it is a subclass rather than a change to the provider
    because a counter is all production needs.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ops: list[str] = []

    async def _tick(self, operation: str) -> None:
        # Recorded before the fault fires, so an operation that raised still appears.
        self.ops.append(operation)
        await super()._tick(operation)


# --------------------------------------------------------------------------------------
# Catalog construction
# --------------------------------------------------------------------------------------

#: The two output names the fixture golden path declares, and which
#: :meth:`MemoryProvider._mint_outputs` therefore produces. Tests hunt for the value
#: behind :data:`SEALED_OUTPUT` when proving that no surface leaks a credential, and for
#: the value behind :data:`PUBLISHED_OUTPUT` when proving that the non-secret path works
#: at all -- a leak sweep that found nothing because nothing was rendered would pass for
#: the wrong reason.
SEALED_OUTPUT = "DATABASE_URL"
PUBLISHED_OUTPUT = "API_TOKEN"

#: The scheme the memory provider mints for any ``*_URL`` output.
#:
#: A constant rather than a literal in each test because the fake's value shape is not
#: the contract -- what it stands for is. Asserting ``postgresql://`` here once meant
#: five tests silently encoded the assumption that the fake pretends to be Postgres, and
#: they all broke the moment it stopped. It is deliberately not a real scheme: a fake
#: credential that looks exactly like a live DSN is one copy-paste from someone filing a
#: security report about test output.
SEALED_VALUE_PREFIX = "memory://"

ALLOW_RULE = PolicyRule(effect="allow", reason="self-service; take one")
DENY_RULE = PolicyRule(
    effect="deny", reason="this capability is not handed out at this installation"
)


def make_path(
    path_id: str,
    *,
    provider: str = "memory",
    enabled: bool = True,
    policy: Sequence[PolicyRule] | None = None,
    inputs: Mapping[str, Any] | None = None,
    outputs: Sequence[BindingOutput] | None = None,
    default_ttl: str = "1h",
    max_ttl: str = "4h",
    warn_before: str = "10m",
    renewable: bool = True,
    max_renewals: int = 2,
    hourly_usd: float = 0.0,
    tags: Sequence[str] | None = None,
    description: str | None = None,
) -> GoldenPath:
    """Build a golden path without going near a YAML file.

    Defaults are the boring ones: a memory-backed path that allows everything and has a
    single required ``name`` input. Every test overrides only the thing it is about.
    """
    return GoldenPath(
        id=path_id,
        name=f"{path_id} (test path)",
        description=description or f"A test golden path called {path_id}.",
        provider=provider,
        enabled=enabled,
        tags=list(tags or []),
        inputs=dict(
            inputs
            if inputs is not None
            else {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 40,
                        "description": "What to call it.",
                    }
                },
            }
        ),
        lease=LeasePolicy(
            default_ttl=Duration(default_ttl),
            max_ttl=Duration(max_ttl),
            warn_before=Duration(warn_before),
            renewable=renewable,
            max_renewals=max_renewals,
        ),
        policy=list(policy) if policy is not None else [ALLOW_RULE],
        cost=CostModel(estimated_hourly_usd=hourly_usd),
        outputs=list(outputs) if outputs is not None else [],
    )


def make_catalog(*paths: GoldenPath, directory: Path | None = None) -> Catalog:
    """Wrap golden paths in the same immutable catalog the loader produces."""
    root = directory or Path("/catalog")
    return Catalog(
        paths={path.id: path for path in paths},
        sources={path.id: root / f"{path.id}.yaml" for path in paths},
        directory=root,
    )


#: The shared catalog every engine-level test runs against.
#:
#: Five paths, each demonstrating one thing: an allow, an approval gate, a flat denial, a
#: path that is switched off, and a path whose provider nobody registered. Keeping them in
#: one catalog rather than one per test means the "list what I can see" behaviours have
#: something to be wrong about.
def build_test_catalog() -> Catalog:
    sandbox = make_path(
        "sandbox",
        inputs={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 40},
                "simulate": {
                    "type": "string",
                    "enum": ["ok", "destroy_failure"],
                    "default": "ok",
                },
            },
        },
        # The memory provider mints exactly these two names, so declaring both -- one
        # sealed, one published -- exercises the whole ``secret:`` flag against the real
        # provider rather than against a stub. ``API_TOKEN`` being the non-secret one is
        # an artefact of the provider's vocabulary, not a recommendation; what the tests
        # care about is that one value comes back in plain text and the other never does.
        outputs=[
            BindingOutput(name=SEALED_OUTPUT, secret=True, description="Sealed."),
            BindingOutput(name=PUBLISHED_OUTPUT, secret=False, description="Published."),
        ],
        default_ttl="5m",
        max_ttl="1h",
        warn_before="1m",
        max_renewals=2,
        tags=["demo"],
    )
    gated = make_path(
        "gated",
        inputs={
            "type": "object",
            "additionalProperties": False,
            "required": ["env", "name"],
            "properties": {
                "env": {"type": "string", "enum": ["dev", "staging", "prod"]},
                "name": {"type": "string", "minLength": 1, "maxLength": 40},
            },
        },
        policy=[
            PolicyRule(
                when='input.env == "prod"',
                effect="require_approval",
                reason="branching production data is a decision a human makes",
            ),
            ALLOW_RULE,
        ],
        outputs=[BindingOutput(name="DATABASE_URL", secret=True)],
        hourly_usd=0.14,
    )
    forbidden = make_path(
        "forbidden",
        inputs={"type": "object", "additionalProperties": False, "properties": {}},
        policy=[DENY_RULE],
    )
    disabled = make_path("switched-off", enabled=False)
    nowhere = make_path("nowhere", provider="ghost")
    return make_catalog(sandbox, gated, forbidden, disabled, nowhere)


# --------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Strip bailment's environment and move out of the repository.

    ``BAILMENT_RESOURCE_PREFIX`` matters most of the three things this removes: the
    reconciler decides what it is allowed to touch by that prefix, so a developer who has
    one exported would get a suite that agrees with itself and proves nothing.
    """
    for name in [key for key in os.environ if key.startswith("BAILMENT_")]:
        monkeypatch.delenv(name, raising=False)
    workdir = tmp_path / "cwd"
    workdir.mkdir(exist_ok=True)
    monkeypatch.chdir(workdir)
    reset_settings_cache()
    yield
    reset_settings_cache()
    reset_logging()


@pytest.fixture
def encryption_key() -> str:
    """A fresh Fernet key per test, so no test can depend on another's ciphertext."""
    return generate_key()


@pytest.fixture
def settings(tmp_path: Path, encryption_key: str) -> Settings:
    """Configuration every fixture below is built from.

    Tokens are configured rather than left empty because a broker nobody can authenticate
    against is a broker whose authorisation rules are untestable, and ``allow_anonymous``
    is off for the same reason it is off in production.
    """
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        encryption_key=encryption_key,
        catalog_dir=tmp_path / "catalog",
        api_tokens="agent-one:agent-token,agent-two:second-agent-token",
        admin_tokens="operator:operator-token",
        allow_anonymous=False,
        worker_id="worker-a",
        lease_tick_seconds=1,
        reconcile_interval_seconds=10,
        max_provision_attempts=3,
        log_level="warning",
        log_format="console",
        public_base_url="http://bailment.test",
    )


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------


@pytest.fixture
async def engine(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    """An in-memory database with the schema created.

    Also installed as :mod:`bailment.db`'s process-wide engine. The API application and
    the OSB router resolve their session factory from there, so a test that left the
    global alone would build an app pointing at ``./bailment.db`` on the developer's disk.
    """
    built = build_engine(settings)
    async with built.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(db_module, "_engine", built)
    yield built
    await built.dispose()


@pytest.fixture
def sessionmaker(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """The session factory, configured exactly as :func:`bailment.db.get_sessionmaker` is.

    ``expire_on_commit=False`` and ``autoflush=False`` are not incidental: the engine
    relies on both, and a test suite that used friendlier settings would hide a
    ``MissingGreenlet`` that only production sees.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_module, "_sessionmaker", factory)
    return factory


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as opened:
        yield opened


@pytest.fixture
async def file_sessionmaker(
    tmp_path: Path, settings: Settings
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A file-backed database, for the tests that need real concurrency.

    An in-memory SQLite lives inside one connection, so :class:`StaticPool` hands the same
    connection to every session and two "concurrent" workers would in fact share one
    transaction. That would make a concurrency test agree with itself for the wrong
    reason. A file has a connection per session and a real lock.
    """
    path = (tmp_path / "concurrent.db").as_posix()
    scoped = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{path}"})
    built = build_engine(scoped)
    async with built.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=built, expire_on_commit=False, autoflush=False)
    await built.dispose()


# --------------------------------------------------------------------------------------
# Engine components
# --------------------------------------------------------------------------------------


@pytest.fixture
def secret_box(encryption_key: str) -> SecretBox:
    return SecretBox([encryption_key])


@pytest.fixture
def provider() -> RecordingProvider:
    """The fake cloud. No latency, so a tick is deterministic and instant."""
    return RecordingProvider(latency_range=(0.0, 0.0))


@pytest.fixture
def registry(provider: RecordingProvider) -> ProviderRegistry:
    built = ProviderRegistry()
    built.register(provider)
    return built


@pytest.fixture
def catalog() -> Catalog:
    return build_test_catalog()


@pytest.fixture
def service(
    session: AsyncSession,
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
    secret_box: SecretBox,
) -> LeaseService:
    return LeaseService(
        session,
        catalog=catalog,
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )


@pytest.fixture
def worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
    secret_box: SecretBox,
) -> ProvisioningWorker:
    return ProvisioningWorker(
        sessionmaker,
        catalog=catalog,
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )


@pytest.fixture
def make_worker(
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
    secret_box: SecretBox,
) -> Callable[..., ProvisioningWorker]:
    """Build extra workers, so two of them can race for one lease."""

    def _make(
        sessions: async_sessionmaker[AsyncSession], worker_id: str, **overrides: Any
    ) -> ProvisioningWorker:
        return ProvisioningWorker(
            sessions,
            catalog=catalog,
            registry=registry,
            settings=settings.model_copy(update={"worker_id": worker_id, **overrides}),
            secret_box=secret_box,
        )

    return _make


@pytest.fixture
def notices() -> list[LeaseNotice]:
    """Everything the ticker tried to tell somebody, in order."""
    return []


@pytest.fixture
def ticker(
    sessionmaker: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    notices: list[LeaseNotice],
) -> LeaseTicker:
    async def collect(notice: LeaseNotice) -> None:
        notices.append(notice)

    return LeaseTicker(sessionmaker, catalog=catalog, settings=settings, notifier=collect)


@pytest.fixture
def make_reconciler(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: ProviderRegistry,
    settings: Settings,
) -> Callable[..., Reconciler]:
    """A reconciler with the arming switches under the test's control.

    Destroying an orphan needs both the global setting and the provider's name in the
    allowlist, so a factory that takes them separately is the only way to prove that
    either one alone does nothing.
    """

    def _make(
        *,
        auto_destroy: bool = False,
        destroy_orphans_for: Sequence[str] = (),
        grace: timedelta = timedelta(minutes=10),
        provider_registry: ProviderRegistry | None = None,
    ) -> Reconciler:
        return Reconciler(
            sessionmaker,
            registry=provider_registry or registry,
            settings=settings.model_copy(update={"reconcile_auto_destroy_orphans": auto_destroy}),
            grace=grace,
            destroy_orphans_for=destroy_orphans_for,
        )

    return _make


# --------------------------------------------------------------------------------------
# Callers
# --------------------------------------------------------------------------------------


@pytest.fixture
def agent() -> Caller:
    return Caller.agent("agent-one", on_behalf_of="dana", session="session-1")


@pytest.fixture
def other_agent() -> Caller:
    return Caller.agent("agent-two", session="session-2")


@pytest.fixture
def operator() -> Caller:
    return Caller.operator("operator")


@pytest.fixture
def human() -> Caller:
    return Caller.human("dana")


@pytest.fixture
def cli_caller() -> Caller:
    return Caller.cli("dana")


@pytest.fixture
def system_caller() -> Caller:
    return Caller.system("bailment")


# --------------------------------------------------------------------------------------
# Workflow helpers
# --------------------------------------------------------------------------------------

RequestLease = Callable[..., Awaitable[ProvisionOutcome]]


@pytest.fixture
def request_lease(service: LeaseService, agent: Caller) -> RequestLease:
    """Ask for a lease as the agent, unless told otherwise."""

    async def _request(
        golden_path_id: str = "sandbox",
        *,
        caller: Caller | None = None,
        inputs: Mapping[str, Any] | None = None,
        ttl: str | None = None,
        idempotency_key: str | None = None,
    ) -> ProvisionOutcome:
        return await service.request_provision(
            caller or agent,
            ProvisionRequest(
                golden_path_id=golden_path_id,
                inputs=dict(inputs if inputs is not None else {"name": "thing"}),
                ttl=ttl,
                idempotency_key=idempotency_key,
            ),
        )

    return _request


@pytest.fixture
def read_lease(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[[str], Awaitable[Lease]]:
    """Re-read a lease in its own session.

    Always a fresh session: the worker, the ticker and the reconciler each commit in their
    own, and asserting against a copy some other session loaded earlier is how a test
    passes while the row on disk says something else.
    """

    async def _read(lease_id: str) -> Lease:
        async with sessionmaker() as opened:
            found = await opened.get(Lease, lease_id)
            assert found is not None, f"lease {lease_id} disappeared"
            # Touch the relationships while the session is open; they are selectin-loaded
            # and reaching for one after the session closes raises from the wrong line.
            _ = found.bindings, found.events, found.approval
            return found

    return _read


@pytest.fixture
def read_events(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[[str], Awaitable[list[AuditEvent]]]:
    async def _read(lease_id: str) -> list[AuditEvent]:
        async with sessionmaker() as opened:
            rows = await opened.execute(
                select(AuditEvent)
                .where(AuditEvent.lease_id == lease_id)
                .order_by(AuditEvent.at.asc(), AuditEvent.id.asc())
            )
            return list(rows.scalars().all())

    return _read


@pytest.fixture
def activate(
    request_lease: RequestLease,
    worker: ProvisioningWorker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[Lease]]:
    """Request a lease and drive it all the way to ACTIVE.

    Returns the committed row rather than a view, because most callers of this want to
    reach straight for ``expires_at`` or a binding.
    """

    async def _activate(golden_path_id: str = "sandbox", **kwargs: Any) -> Lease:
        outcome = await request_lease(golden_path_id, **kwargs)
        await worker.tick()
        async with sessionmaker() as opened:
            lease = await opened.get(Lease, outcome.lease.id)
            assert lease is not None
            _ = lease.bindings, lease.events
            assert lease.state == "active", f"lease did not activate: {lease.state}"
            return lease

    return _activate


@pytest.fixture
def sealed_values(secret_box: SecretBox) -> Callable[[Lease], dict[str, str]]:
    """Open a lease's binding, so a test knows the exact string that must never surface.

    This is the only place in the suite that decrypts anything, and it exists so that leak
    hunts search for the real credential rather than for a plausible-looking substring.
    """

    def _values(lease: Lease) -> dict[str, str]:
        live = [binding for binding in lease.bindings if binding.revoked_at is None]
        assert live, f"lease {lease.id} has no live binding"
        return secret_box.open(live[0].ciphertext)

    return _values


@pytest.fixture
def age_lease(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[None]]:
    """Move a lease's timestamps into the past.

    Sleeping is not an option -- the grace period is ten minutes -- and moving the clock
    would mean patching :func:`bailment.models.utcnow` in six modules. Writing older
    timestamps produces exactly the row a real aged lease has.
    """

    async def _age(lease_id: str, *, by: timedelta) -> None:
        async with sessionmaker() as opened:
            lease = await opened.get(Lease, lease_id)
            assert lease is not None
            lease.created_at = lease.created_at - by
            if lease.activated_at is not None:
                lease.activated_at = lease.activated_at - by
            if lease.claimed_at is not None:
                lease.claimed_at = lease.claimed_at - by
            await opened.commit()

    return _age
