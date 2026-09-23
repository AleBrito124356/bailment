"""The shared sandbox store: one fake cloud that every bailment process agrees about.

The bug these exist for was destructive and quiet. The memory provider kept its resources
in a dict, so a reconciler running anywhere but the worker's process -- ``bailment
reconcile`` in a second terminal, or the compose ``api`` container answering the
dashboard's "Reconcile now" -- asked an empty provider what existed, and marked healthy
leases RELEASED and revoked their bindings while the resources were alive next door.

A "second process" here is a second :class:`MemoryProvider` instance over the same
database file. That is exactly what a second process has: its own provider object, its own
connections, and nothing in common with the first but the database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.catalog.loader import Catalog
from bailment.config import Settings
from bailment.db import build_engine
from bailment.engine.reconciler import Reconciler
from bailment.engine.service import Caller, LeaseService, ProvisionRequest
from bailment.engine.worker import ProvisioningWorker
from bailment.models import AuditEvent, Base, Lease, MemoryResourceRecord
from bailment.providers.base import ProviderError, ResourceStatus
from bailment.providers.memory import (
    InProcessStore,
    MemoryProvider,
    SqlResourceStore,
    default_memory_provider,
)
from bailment.providers.registry import ProviderRegistry
from bailment.secrets import SecretBox
from bailment.states import LeaseState
from conftest import SEALED_OUTPUT, SEALED_VALUE_PREFIX

OLD = timedelta(minutes=20)


@pytest.fixture
async def shared_db(
    tmp_path: Path, settings: Settings
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """One database file, as every process in a deployment would share."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'shared.db').as_posix()}"
    engine = build_engine(settings.model_copy(update={"database_url": url}))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


def process(factory: async_sessionmaker[AsyncSession], box: SecretBox) -> MemoryProvider:
    """A memory provider as a separate bailment process would build it."""
    return MemoryProvider(
        latency_range=(0.0, 0.0),
        store=SqlResourceStore(sessionmaker=lambda: factory, secret_box=lambda: box),
    )


def registry_of(provider: MemoryProvider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(provider)
    return registry


async def request_and_activate(
    factory: async_sessionmaker[AsyncSession],
    *,
    catalog: Catalog,
    provider: MemoryProvider,
    settings: Settings,
    box: SecretBox,
    inputs: dict[str, str] | None = None,
) -> str:
    registry = registry_of(provider)
    async with factory() as session:
        outcome = await LeaseService(
            session, catalog=catalog, registry=registry, settings=settings, secret_box=box
        ).request_provision(
            Caller.agent("agent-one", session="s-1"),
            ProvisionRequest(golden_path_id="sandbox", inputs=inputs or {"name": "shared"}),
        )
    worker = ProvisioningWorker(
        factory, catalog=catalog, registry=registry, settings=settings, secret_box=box
    )
    assert (await worker.tick()).provisioned == [outcome.lease.id]
    return outcome.lease.id


async def age(factory: async_sessionmaker[AsyncSession], lease_id: str) -> None:
    async with factory() as session:
        lease = await session.get(Lease, lease_id)
        assert lease is not None
        lease.created_at = lease.created_at - OLD
        if lease.activated_at is not None:
            lease.activated_at = lease.activated_at - OLD
        await session.commit()
        await session.execute(update(MemoryResourceRecord).values(created_at=lease.created_at))
        await session.commit()


# --------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------


async def test_two_processes_on_one_database_see_one_fake_cloud(
    shared_db: async_sessionmaker[AsyncSession], secret_box: SecretBox
) -> None:
    api_side = process(shared_db, secret_box)
    worker_side = process(shared_db, secret_box)

    created = await worker_side.create(
        external_name="bailment-sandbox-0000aaaa",
        inputs={"name": "x"},
        declared_outputs=["SANDBOX_URL", "SANDBOX_ID"],
    )
    ref = created.provider_ref

    assert await api_side.exists(external_name="bailment-sandbox-0000aaaa", provider_ref=ref) is (
        ResourceStatus.EXISTS
    )
    assert [r.external_name for r in await api_side.list_managed()] == ["bailment-sandbox-0000aaaa"]

    # A retry from the other process adopts, and gets the credential the first one minted.
    adopted = await api_side.create(
        external_name="bailment-sandbox-0000aaaa", inputs={"name": "x"}, declared_outputs=[]
    )
    assert adopted.adopted is True
    assert adopted.provider_resource_id == created.provider_resource_id
    assert adopted.outputs == created.outputs

    await api_side.destroy(external_name="bailment-sandbox-0000aaaa", provider_ref=ref)
    assert await worker_side.exists(
        external_name="bailment-sandbox-0000aaaa", provider_ref=ref
    ) is (ResourceStatus.GONE)


async def test_the_minted_credential_is_sealed_at_rest(
    shared_db: async_sessionmaker[AsyncSession], secret_box: SecretBox, tmp_path: Path
) -> None:
    """Fake, and still shaped exactly like a real DSN. Nothing like it sits in plain text."""
    provider = process(shared_db, secret_box)
    created = await provider.create(
        external_name="bailment-sandbox-0000bbbb", inputs={}, declared_outputs=["SANDBOX_URL"]
    )
    value = created.outputs["SANDBOX_URL"]
    assert value.startswith(SEALED_VALUE_PREFIX)

    async with shared_db() as session:
        row = (await session.execute(select(MemoryResourceRecord))).scalars().one()
    assert row.sealed_outputs is not None
    assert value.encode() not in row.sealed_outputs
    assert secret_box.open(row.sealed_outputs) == {"SANDBOX_URL": value}

    # The file itself, WAL included, read the way anybody with disk access would.
    on_disk = [(path.name, path.read_bytes()) for path in sorted(tmp_path.glob("shared.db*"))]  # noqa: ASYNC240
    assert on_disk
    for name, raw in on_disk:
        assert SEALED_VALUE_PREFIX.encode() not in raw, name


async def test_an_unreachable_store_is_unknown_and_never_gone(
    tmp_path: Path, settings: Settings, secret_box: SecretBox
) -> None:
    """The provider contract: a failed lookup is not evidence of absence. A database with
    no ``memory_resources`` table stands in for one that is down."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'empty.db').as_posix()}"
    engine = build_engine(settings.model_copy(update={"database_url": url}))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    provider = process(factory, secret_box)
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.exists(external_name="bailment-sandbox-0000cccc", provider_ref={})
        assert caught.value.retryable is True
        assert "unknown" in caught.value.message
        with pytest.raises(ProviderError):
            await provider.list_managed()
    finally:
        await engine.dispose()


async def test_the_sync_hooks_refuse_a_shared_store_and_name_the_way_that_works(
    shared_db: async_sessionmaker[AsyncSession], secret_box: SecretBox
) -> None:
    provider = process(shared_db, secret_box)
    with pytest.raises(RuntimeError, match=r"await provider\.avanish"):
        provider.vanish("bailment-sandbox-0000dddd")
    with pytest.raises(RuntimeError, match="bailment sandbox"):
        provider.snapshot()


def test_the_provider_the_registry_hands_out_uses_the_shared_store() -> None:
    """The one every real process builds. A bare MemoryProvider() stays in-process."""
    assert isinstance(default_memory_provider().store, SqlResourceStore)
    assert default_memory_provider().store.shared is True
    assert isinstance(MemoryProvider().store, InProcessStore)


# --------------------------------------------------------------------------------------
# The regression, and both directions of drift across processes
# --------------------------------------------------------------------------------------


async def test_a_reconciler_in_another_process_leaves_a_live_lease_alone(
    shared_db: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    """The bug, reproduced as the audit found it: a healthy lease past the grace period,
    and a reconciler built in a different process. It must find nothing to do."""
    worker_side = process(shared_db, secret_box)
    lease_id = await request_and_activate(
        shared_db, catalog=catalog, provider=worker_side, settings=settings, box=secret_box
    )
    await age(shared_db, lease_id)

    terminal_side = process(shared_db, secret_box)
    summary = await Reconciler(
        shared_db, registry=registry_of(terminal_side), settings=settings
    ).run()

    assert summary.drift_found == 0
    assert summary.orphans_found == 0
    assert summary.resources_seen == 1
    assert summary.leases_checked == 1
    async with shared_db() as session:
        lease = await session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == LeaseState.ACTIVE.value
        assert all(binding.revoked_at is None for binding in lease.bindings)


async def test_an_in_process_store_in_another_process_is_exactly_the_old_bug(
    shared_db: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    """The control for the test above: the same setup with a per-process dict reports the
    healthy lease as drift. If this ever stops failing the lease, the test above has
    stopped proving anything."""
    worker_side = process(shared_db, secret_box)
    lease_id = await request_and_activate(
        shared_db, catalog=catalog, provider=worker_side, settings=settings, box=secret_box
    )
    await age(shared_db, lease_id)

    lonely = MemoryProvider(latency_range=(0.0, 0.0))  # an empty dict, in "another process"
    summary = await Reconciler(shared_db, registry=registry_of(lonely), settings=settings).run()

    assert summary.drift_found == 1
    async with shared_db() as session:
        lease = await session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == LeaseState.RELEASED.value


async def test_a_resource_vanished_from_another_process_is_found_as_drift(
    shared_db: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    """``bailment sandbox vanish`` then ``bailment reconcile``: ACTIVE, then UNKNOWN (we no
    longer know), then RELEASED (we have established it is gone), each in the audit."""
    lease_id = await request_and_activate(
        shared_db,
        catalog=catalog,
        provider=process(shared_db, secret_box),
        settings=settings,
        box=secret_box,
    )
    await age(shared_db, lease_id)

    async with shared_db() as session:
        external_name = (await session.get(Lease, lease_id)).external_name  # type: ignore[union-attr]
    assert external_name is not None
    assert await process(shared_db, secret_box).avanish(external_name) is True

    summary = await Reconciler(
        shared_db, registry=registry_of(process(shared_db, secret_box)), settings=settings
    ).run()

    assert summary.drift_found == 1
    async with shared_db() as session:
        lease = await session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == LeaseState.RELEASED.value
        assert all(binding.revoked_at is not None for binding in lease.bindings)
        moves = [
            (event.from_state, event.to_state)
            for event in (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.lease_id == lease_id,
                        AuditEvent.actor == "bailment:reconciler",
                    )
                    .order_by(AuditEvent.at, AuditEvent.id)
                )
            )
            .scalars()
            .all()
        ]
    assert ("active", "unknown") in moves
    assert ("unknown", "released") in moves


async def test_a_planted_orphan_is_reported_then_destroyed_only_when_armed(
    shared_db: async_sessionmaker[AsyncSession], settings: Settings, secret_box: SecretBox
) -> None:
    planter = process(shared_db, secret_box)
    await planter.aplant_orphan("bailment-orphan-0000eeee", age=timedelta(minutes=15))
    await planter.aplant_orphan("bailment-orphan-0000ffff")  # too young to judge

    sweeper = process(shared_db, secret_box)
    report = await Reconciler(shared_db, registry=registry_of(sweeper), settings=settings).run()
    assert [o.external_name for o in report.providers[0].orphans] == ["bailment-orphan-0000eeee"]
    assert report.providers[0].within_grace == 1
    assert set(await sweeper.asnapshot()) == {
        "bailment-orphan-0000eeee",
        "bailment-orphan-0000ffff",
    }

    armed = await Reconciler(
        shared_db,
        registry=registry_of(sweeper),
        settings=settings.model_copy(update={"reconcile_auto_destroy_orphans": True}),
        destroy_orphans_for=["memory"],
    ).run()
    assert armed.orphans_destroyed == 1
    assert set(await planter.asnapshot()) == {"bailment-orphan-0000ffff"}


async def test_a_destroy_failure_orphan_ends_when_relented_and_re_driven(
    shared_db: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    """The demo orphan used to be permanent: relent was reachable from no surface and
    retry-teardown left the lease in a state no worker picks up. Now the whole story ends."""
    worker_side = process(shared_db, secret_box)
    registry = registry_of(worker_side)
    lease_id = await request_and_activate(
        shared_db,
        catalog=catalog,
        provider=worker_side,
        settings=settings,
        box=secret_box,
        inputs={"name": "stuck", "simulate": "destroy_failure"},
    )
    operator = Caller.operator("operator")
    async with shared_db() as session:
        await LeaseService(
            session, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
        ).revoke(lease_id, operator, reason="done")

    worker = ProvisioningWorker(
        shared_db, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
    )
    for _ in range(settings.max_provision_attempts + 1):
        async with shared_db() as session:
            lease = await session.get(Lease, lease_id)
            assert lease is not None
            if lease.state == LeaseState.ORPHANED.value:
                break
            lease.next_attempt_at = None
            await session.commit()
        await worker.tick()

    async with shared_db() as session:
        lease = await session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == LeaseState.ORPHANED.value
        external_name = lease.external_name
    assert external_name is not None

    # From "another terminal": lift the blocker, then re-drive.
    assert await process(shared_db, secret_box).arelent(external_name) == 1
    async with shared_db() as session:
        view = await LeaseService(
            session, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
        ).retry_teardown(lease_id, operator)
    assert view.state is LeaseState.DEPROVISIONING

    assert (await worker.tick()).released == [lease_id]
    assert await worker_side.asnapshot() == {}


async def test_the_sealed_value_never_appears_in_the_store_listing(
    shared_db: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    """``bailment sandbox ls`` reads :meth:`ResourceStore.all`, which must not open the
    envelope -- listing what exists needs no key and shows no credential."""
    provider = process(shared_db, secret_box)
    await request_and_activate(
        shared_db, catalog=catalog, provider=provider, settings=settings, box=secret_box
    )
    listed = await provider.store.all()
    assert len(listed) == 1
    assert listed[0].outputs == {}
    assert SEALED_OUTPUT not in repr(listed[0].outputs)
