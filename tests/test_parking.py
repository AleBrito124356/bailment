"""Being honest about what cannot happen yet, and two configuration traps.

A request for a golden path whose provider is registered but not configured is accepted --
the misconfiguration is the operator's, and fixing it should not require the requester to
ask again -- and then parked until the credential turns up. Before this, nobody was told:
the REST response carried no notice, the MCP result said provisioning "usually takes a few
seconds", and meanwhile every worker pass appended two audit rows to the parked lease, about
7,800 a day each, recording nothing new.

The traps: ``BAILMENT_CATALOG_DIR=`` (blank, exactly as ``.env.example`` shipped it) pointed
the catalog at the current directory; and ``bailment serve`` logged "no lease engine in
this process" from the very process that was running the engine.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.types import TextContent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.catalog.loader import Catalog, load_catalog
from bailment.config import PACKAGED_CATALOG_DIR, Settings, reset_settings_cache
from bailment.engine.service import Caller, LeaseService, ProvisionRequest
from bailment.engine.worker import ProvisioningWorker
from bailment.logging import configure_logging
from bailment.main import create_app
from bailment.mcp.server import TOOL_LEASE_STATUS, build_mcp_server
from bailment.models import AuditEvent, Lease
from bailment.providers.memory import MemoryProvider
from bailment.providers.registry import ProviderRegistry
from bailment.secrets import SecretBox
from bailment.states import LeaseState
from conftest import ALLOW_RULE, DENY_RULE, make_catalog, make_path

AGENT = {"Authorization": "Bearer agent-token"}


@pytest.fixture
def neon_path_catalog() -> Catalog:
    """One path on a provider called ``neon`` -- a real built-in name, so the missing
    settings are the real ``BAILMENT_NEON_*`` variables -- backed by the fake."""
    return make_catalog(make_path("postgres", provider="neon", policy=[ALLOW_RULE]))


@pytest.fixture
def unconfigured() -> MemoryProvider:
    provider = MemoryProvider(name="neon", latency_range=(0.0, 0.0))
    provider.set_unavailable("BAILMENT_NEON_API_KEY is not set")
    return provider


@pytest.fixture
def parked_registry(unconfigured: MemoryProvider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(unconfigured)
    return registry


async def request(
    sessionmaker: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
) -> Any:  # noqa: ANN401
    async with sessionmaker() as session:
        return await LeaseService(
            session, catalog=catalog, registry=registry, settings=settings
        ).request_provision(
            Caller.agent("agent-one", session="s-1"),
            ProvisionRequest(golden_path_id="postgres", inputs={"name": "try-it"}),
        )


# --------------------------------------------------------------------------------------
# The caller is told
# --------------------------------------------------------------------------------------


async def test_a_request_for_an_unconfigured_provider_says_it_is_parked_and_why(
    sessionmaker: async_sessionmaker[AsyncSession],
    neon_path_catalog: Catalog,
    parked_registry: ProviderRegistry,
    settings: Settings,
) -> None:
    outcome = await request(sessionmaker, neon_path_catalog, parked_registry, settings)

    assert outcome.lease.state is LeaseState.PENDING
    assert outcome.parked is not None
    # The real variable names, because they are what somebody has to go and set.
    assert "BAILMENT_NEON_API_KEY, BAILMENT_NEON_PROJECT_ID are not set" in outcome.parked
    assert "parked" in outcome.parked
    assert outcome.parked in outcome.notices
    assert outcome.as_dict()["parked"] == outcome.parked


async def test_a_configured_provider_gets_no_parked_notice(
    request_lease: Callable[..., Any],
) -> None:
    outcome = await request_lease()
    assert outcome.parked is None
    assert not any("parked" in notice for notice in outcome.notices)


async def test_a_denied_request_is_not_also_told_it_is_parked(
    sessionmaker: async_sessionmaker[AsyncSession],
    parked_registry: ProviderRegistry,
    settings: Settings,
) -> None:
    """A denial is final. Adding "and it will start once configured" would contradict it."""

    catalog = make_catalog(make_path("postgres", provider="neon", policy=[DENY_RULE]))
    outcome = await request(sessionmaker, catalog, parked_registry, settings)
    assert outcome.lease.state is LeaseState.REJECTED
    assert outcome.parked is None


async def test_the_parked_notice_reaches_a_rest_caller(
    settings: Settings,
    neon_path_catalog: Catalog,
    parked_registry: ProviderRegistry,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    application = create_app(settings=settings, catalog=neon_path_catalog, registry=parked_registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://broker.test"
    ) as client:
        response = await client.post(
            "/api/v1/leases",
            headers=AGENT,
            json={"golden_path": "postgres", "inputs": {"name": "try-it"}},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["lease"]["state"] == "pending"
    assert any("BAILMENT_NEON_API_KEY" in notice for notice in body["notices"])


async def test_an_agent_is_told_parked_not_a_few_seconds_and_polling_says_so_too(
    sessionmaker: async_sessionmaker[AsyncSession],
    neon_path_catalog: Catalog,
    parked_registry: ProviderRegistry,
    unconfigured: MemoryProvider,
    settings: Settings,
) -> None:
    mcp = build_mcp_server(
        sessionmaker, catalog=neon_path_catalog, registry=parked_registry, settings=settings
    )
    caller = Caller.agent("agent-one", session="s-1")

    result = await mcp._dispatch("bailment_provision_postgres", {"name": "try-it"}, caller)
    text = result.content[0]
    assert isinstance(text, TextContent)
    assert text.text.startswith("PARKED.")
    assert "usually takes a few seconds" not in text.text
    assert "BAILMENT_NEON_API_KEY" in text.text
    # The reason is in the body; it must not be repeated under "Also worth knowing".
    assert text.text.count("BAILMENT_NEON_API_KEY") == 1
    lease_id = text.text.split("lease_id: ")[1].split("\n")[0]

    polled = await mcp._dispatch(TOOL_LEASE_STATUS, {"lease_id": lease_id}, caller)
    assert isinstance(polled.content[0], TextContent)
    assert polled.content[0].text.startswith("PARKED.")

    # The operator sets the credential: the very next poll stops saying PARKED.
    unconfigured.set_unavailable(None)
    polled = await mcp._dispatch(TOOL_LEASE_STATUS, {"lease_id": lease_id}, caller)
    assert isinstance(polled.content[0], TextContent)
    assert polled.content[0].text.startswith("QUEUED.")


# --------------------------------------------------------------------------------------
# The audit log is not a heartbeat
# --------------------------------------------------------------------------------------


async def test_a_parked_lease_writes_a_constant_number_of_audit_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    neon_path_catalog: Catalog,
    parked_registry: ProviderRegistry,
    unconfigured: MemoryProvider,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    """25 passes against a parked lease. It used to grow by two rows a pass."""
    outcome = await request(sessionmaker, neon_path_catalog, parked_registry, settings)
    worker = ProvisioningWorker(
        sessionmaker,
        catalog=neon_path_catalog,
        registry=parked_registry,
        settings=settings,
        secret_box=secret_box,
    )

    async def rows() -> list[str]:
        async with sessionmaker() as session:
            found = await session.execute(
                select(AuditEvent.action)
                .where(AuditEvent.lease_id == outcome.lease.id)
                .order_by(AuditEvent.at, AuditEvent.id)
            )
            return list(found.scalars().all())

    async def due_now() -> None:
        async with sessionmaker() as session:
            lease = await session.get(Lease, outcome.lease.id)
            assert lease is not None
            lease.next_attempt_at = None
            await session.commit()

    counts: list[int] = []
    for _ in range(25):
        await due_now()
        tick = await worker.tick()
        assert tick.skipped == 1
        counts.append(len(await rows()))

    assert set(counts) == {3}
    assert await rows() == ["requested", "queued", "provision_failed"]
    async with sessionmaker() as session:
        lease = await session.get(Lease, outcome.lease.id)
        assert lease is not None
        assert lease.attempts == 0  # every pass still hands its attempt back
        assert lease.state == LeaseState.PENDING.value

    # Configured at last: the claim that does work is recorded, and the lease provisions.
    unconfigured.set_unavailable(None)
    await due_now()
    assert (await worker.tick()).provisioned == [outcome.lease.id]
    assert (await rows())[-3:] == ["claimed", "provisioning", "activated"]


async def test_a_park_for_a_new_reason_is_recorded_again(
    sessionmaker: async_sessionmaker[AsyncSession],
    neon_path_catalog: Catalog,
    parked_registry: ProviderRegistry,
    unconfigured: MemoryProvider,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    outcome = await request(sessionmaker, neon_path_catalog, parked_registry, settings)
    worker = ProvisioningWorker(
        sessionmaker,
        catalog=neon_path_catalog,
        registry=parked_registry,
        settings=settings,
        secret_box=secret_box,
    )
    await worker.tick()

    empty = ProviderRegistry()  # now the provider is not even registered
    other = ProvisioningWorker(
        sessionmaker,
        catalog=neon_path_catalog,
        registry=empty,
        settings=settings,
        secret_box=secret_box,
    )
    async with sessionmaker() as session:
        lease = await session.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.next_attempt_at = None
        await session.commit()
    await other.tick()

    async with sessionmaker() as session:
        parks = await session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.lease_id == outcome.lease.id,
                AuditEvent.action == "provision_failed",
            )
        )
        assert parks.scalar_one() == 2


# --------------------------------------------------------------------------------------
# Configuration and logging traps
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_catalog_dir_means_the_shipped_catalog_not_the_working_directory(
    blank: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exactly what .env.example ships. The working directory here holds an unrelated YAML
    file, which the blank value used to make bailment try to load as a golden path."""
    (Path.cwd() / "providers.e2e.yaml").write_text("id: not-a-golden-path\n", encoding="utf-8")
    monkeypatch.setenv("BAILMENT_CATALOG_DIR", blank)
    reset_settings_cache()

    settings = Settings()

    assert settings.catalog_dir == PACKAGED_CATALOG_DIR
    loaded = load_catalog(settings.catalog_dir)
    assert sorted(path.id for path in loaded.all()) == [
        "dns-record",
        "postgres",
        "redis",
        "sandbox",
    ]


def test_an_explicit_catalog_dir_is_still_honoured(tmp_path: Path) -> None:
    assert Settings(catalog_dir=tmp_path).catalog_dir == tmp_path


def _events(buffer: io.StringIO) -> list[str]:
    lines = [line for line in buffer.getvalue().splitlines() if line.startswith("{")]
    return [json.loads(line)["event"] for line in lines]


async def test_an_app_whose_engine_runs_beside_it_says_so_instead_of_warning(
    settings: Settings, catalog: Catalog, registry: ProviderRegistry, sessionmaker: Any
) -> None:
    """``bailment serve`` supervises the worker, ticker and reconciler next to the app and
    tells it so. The app used to warn that nothing in the process expires a lease."""
    buffer = io.StringIO()
    configure_logging("info", "json", force=True, stream=buffer)
    application = create_app(
        settings=settings,
        catalog=catalog,
        registry=registry,
        engine_supervised=("worker", "ticker", "reconciler"),
    )
    async with application.router.lifespan_context(application):
        pass

    events = _events(buffer)
    assert not any(event.startswith("no lease engine in this process") for event in events)
    assert "lease engine supervised beside the application in this process" in events


async def test_an_app_with_no_engine_anywhere_still_warns(
    settings: Settings, catalog: Catalog, registry: ProviderRegistry, sessionmaker: Any
) -> None:
    buffer = io.StringIO()
    configure_logging("info", "json", force=True, stream=buffer)
    application = create_app(settings=settings, catalog=catalog, registry=registry)
    async with application.router.lifespan_context(application):
        pass
    assert any(event.startswith("no lease engine in this process") for event in _events(buffer))
