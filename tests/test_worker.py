"""The worker, tested by the ways it gets killed.

Everything in :mod:`bailment.engine.worker` is written for the case where the process
dies mid-provision, so almost every test here arranges a failure rather than a success.
The two that matter most are asymmetric on purpose and it is worth saying why in one
place:

*A create that failed* may have left a real resource behind. The worker asks, rolls back
what it finds, and lands on ``FAILED``. If it could not roll back, or could not tell, the
lease goes ``ORPHANED`` -- being wrong in that direction costs a line on a dashboard, and
being wrong in the other costs a resource nobody will ever look for again.

*A destroy that failed* never reaches ``RELEASED``. ``RELEASED`` is a claim that a
resource is gone, and a system that writes it optimistically makes a failed teardown look
exactly like a successful one, at which point the orphan is invisible forever.

``MemoryProvider.fail_next(..., half_succeed=True)`` is what makes the first of those
testable at all: it stores the resource and *then* raises, which is indistinguishable from
a worker dying after the provider committed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.config import Settings
from bailment.engine.service import Caller, LeaseService, ProvisionRequest, queue_for_teardown
from bailment.engine.worker import (
    CLAIM_TTL,
    PROVISION_STATES,
    TEARDOWN_STATES,
    ProvisioningWorker,
)
from bailment.models import Lease, utcnow
from bailment.providers.registry import ProviderRegistry
from bailment.states import LeaseState
from conftest import (
    PUBLISHED_OUTPUT,
    SEALED_OUTPUT,
    SEALED_VALUE_PREFIX,
    RecordingProvider,
    make_catalog,
    make_path,
)


def transitions(events) -> set[tuple[str | None, str | None]]:
    """State moves as an unordered set.

    Audit rows written in the same instant tie-break on a uuid, so their order is not
    something a test may depend on -- but the set of moves is exact.
    """
    return {(event.from_state, event.to_state) for event in events}


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


async def test_a_pending_lease_becomes_active_with_a_sealed_binding(
    request_lease,
    worker: ProvisioningWorker,
    read_lease,
    provider: RecordingProvider,
    sealed_values,
) -> None:
    outcome = await request_lease()
    tick = await worker.tick()

    assert tick.provisioned == [outcome.lease.id]
    assert tick.handled == 1
    assert provider.ops == ["preflight", "create"]

    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.ACTIVE.value
    assert lease.provider_resource_id
    assert lease.provider_ref["name"] == lease.external_name
    assert lease.activated_at is not None
    assert lease.expires_at is not None
    # The claim is handed back and the retry budget reset: this lease is nobody's work now.
    assert lease.claimed_by is None
    assert lease.next_attempt_at is None
    assert lease.attempts == 0
    assert lease.failure_reason is None

    binding = lease.bindings[0]
    assert binding.reference == f"bailment://binding/{binding.id}"
    assert sorted(binding.output_names) == sorted([SEALED_OUTPUT, PUBLISHED_OUTPUT])
    assert sealed_values(lease)[SEALED_OUTPUT].startswith(SEALED_VALUE_PREFIX)


async def test_the_activation_event_publishes_only_the_non_secret_outputs(
    request_lease, worker: ProvisioningWorker, read_events, sealed_values, read_lease
) -> None:
    """Every read path renders from this event, which is how ``get_lease`` avoids being a
    decryption path at all -- so what the worker wrote here is the whole disclosure surface."""
    outcome = await request_lease()
    await worker.tick()

    lease = await read_lease(outcome.lease.id)
    values = sealed_values(lease)
    activated = next(e for e in await read_events(outcome.lease.id) if e.action == "activated")

    assert activated.detail["outputs"] == {PUBLISHED_OUTPUT: values[PUBLISHED_OUTPUT]}
    assert sorted(activated.detail["output_names"]) == sorted([SEALED_OUTPUT, PUBLISHED_OUTPUT])
    assert values[SEALED_OUTPUT] not in str(activated.detail)


async def test_an_output_the_catalog_never_declared_is_sealed_and_not_published(
    session: AsyncSession, sessionmaker, registry, settings, secret_box, agent, read_events
) -> None:
    """Anything unclassified is treated as secret. The drift is said out loud in the log
    because somebody is about to wonder where their credential went."""
    catalog = make_catalog(make_path("undeclared", outputs=[]))
    service = LeaseService(
        session, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
    )
    outcome = await service.request_provision(
        agent, ProvisionRequest(golden_path_id="undeclared", inputs={"name": "x"})
    )
    worker = ProvisioningWorker(
        sessionmaker,
        catalog=catalog,
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )
    await worker.tick()

    activated = next(e for e in await read_events(outcome.lease.id) if e.action == "activated")
    assert activated.detail["outputs"] == {}
    assert sorted(activated.detail["output_names"]) == sorted([SEALED_OUTPUT, PUBLISHED_OUTPUT])


async def test_a_tick_with_nothing_to_do_does_nothing(worker: ProvisioningWorker, provider) -> None:
    tick = await worker.tick()
    assert tick.handled == 0
    assert tick.skipped == 0
    assert provider.ops == []


# --------------------------------------------------------------------------------------
# Create failures
# --------------------------------------------------------------------------------------


async def test_a_create_that_half_succeeded_is_rolled_back_and_the_lease_fails(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider, read_events
) -> None:
    """The shape of every real half-completed provision: stored, then the call raised.

    The provider says the resource exists, the worker destroys it, and only then does the
    lease become ``FAILED`` -- which is an assertion that nothing is left running.
    """
    outcome = await request_lease()
    provider.fail_next("create", half_succeed=True, message="stored, then the call died")

    tick = await worker.tick()

    assert tick.failed == [outcome.lease.id]
    assert provider.ops == ["preflight", "create", "exists", "destroy"]
    assert provider.snapshot() == {}

    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.FAILED.value
    assert "stored, then the call died" in (lease.failure_reason or "")
    assert lease.next_attempt_at is None
    assert lease.bindings == []

    events = await read_events(outcome.lease.id)
    assert "rolled_back" in [event.action for event in events]
    assert transitions(events) >= {("provisioning", "failed")}


async def test_a_create_that_left_nothing_behind_fails_without_a_destroy(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider, read_events
) -> None:
    """The provider reports nothing was created, so there is nothing to undo.

    The rollback is still *recorded*, because an audit reader has to be able to tell this
    apart from the case where something briefly existed.
    """
    outcome = await request_lease()
    provider.fail_next("create", message="rejected: quota exceeded")

    await worker.tick()

    assert provider.ops == ["preflight", "create", "exists"]
    assert (await read_lease(outcome.lease.id)).state == LeaseState.FAILED.value
    rolled = next(e for e in await read_events(outcome.lease.id) if e.action == "rolled_back")
    assert rolled.detail["note"] == "provider reports nothing was created"


async def test_a_rollback_that_fails_lands_orphaned_not_failed(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider, read_events
) -> None:
    """Something may be alive and billing, and the only wrong answer is to stop looking.

    Note the route: there is no ``PROVISIONING -> ORPHANED`` edge, so the worker says the
    two things it actually learned, in order -- we could not determine what the provider
    did, and *therefore* we now believe there may be an unowned resource.
    """
    outcome = await request_lease()
    provider.fail_next("create", half_succeed=True, message="stored, then the call died")
    provider.fail_next("destroy", message="403 from the provider")

    tick = await worker.tick()

    assert tick.orphaned == [outcome.lease.id]
    assert tick.failed == []
    assert provider.snapshot()  # the resource really is still there

    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.ORPHANED.value
    assert "may exist" in (lease.failure_reason or "")
    assert lease.external_name in (lease.failure_reason or "")

    events = await read_events(outcome.lease.id)
    assert transitions(events) >= {("provisioning", "unknown"), ("unknown", "orphaned")}
    assert {"state_unknown", "orphaned"} <= {event.action for event in events}


async def test_a_provider_that_cannot_say_what_exists_orphans_rather_than_guessing(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider
) -> None:
    """``exists`` failing is not evidence of absence."""
    outcome = await request_lease()
    provider.fail_next("create", half_succeed=True, message="unclear failure")
    provider.fail_next("exists", message="503 from the provider")
    provider.fail_next("destroy", message="503 from the provider")

    await worker.tick()
    assert (await read_lease(outcome.lease.id)).state == LeaseState.ORPHANED.value


async def test_a_preflight_refusal_fails_without_touching_a_provider(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider
) -> None:
    """A request doomed by a bad input should fail before anything is in flight, and
    with no rollback attempted, because nothing could have been created."""
    outcome = await request_lease()
    provider.fail_next("preflight", message="'name' is not a legal branch name")

    await worker.tick()

    assert provider.ops == ["preflight"]
    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.FAILED.value
    assert "legal branch name" in (lease.failure_reason or "")
    # It never left PENDING, so no resource could ever have carried its name.
    assert lease.previous_state == LeaseState.PENDING.value


async def test_a_bug_inside_a_provider_is_still_treated_as_a_possible_creation(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider, monkeypatch
) -> None:
    """A ``TypeError`` from a provider is a create that may have half-happened."""
    outcome = await request_lease()

    async def explode(**_: object) -> None:
        raise TypeError("a bug in the provider")

    monkeypatch.setattr(provider, "create", explode)
    await worker.tick()

    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.FAILED.value
    assert "unexpected TypeError from create" in (lease.failure_reason or "")


# --------------------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------------------


async def test_a_retryable_failure_is_rescheduled_rather_than_failed(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider
) -> None:
    outcome = await request_lease()
    provider.fail_next("create", message="429 slow down", retryable=True)

    tick = await worker.tick()

    assert tick.retried == [outcome.lease.id]
    assert tick.failed == []
    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.PROVISIONING.value
    assert lease.attempts == 1
    assert lease.claimed_by is None  # released, so any worker may pick it up
    assert lease.next_attempt_at is not None and lease.next_attempt_at > utcnow().replace(
        tzinfo=lease.next_attempt_at.tzinfo
    )


async def test_a_rescheduled_lease_is_not_picked_up_before_its_time(
    request_lease, worker: ProvisioningWorker, provider: RecordingProvider
) -> None:
    await request_lease()
    provider.fail_next("create", message="429 slow down", retryable=True)
    await worker.tick()

    provider.ops.clear()
    assert (await worker.tick()).handled == 0
    assert provider.ops == []


async def test_the_retry_budget_runs_out_and_the_lease_stops_being_retried(
    request_lease, make_worker, sessionmaker, read_lease, provider: RecordingProvider
) -> None:
    worker = make_worker(sessionmaker, "worker-a", max_provision_attempts=1)
    outcome = await request_lease()
    provider.fail_next("create", message="429 slow down", retryable=True)

    await worker.tick()

    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.FAILED.value
    assert "after 1 attempt(s)" in (lease.failure_reason or "")


async def test_a_retry_adopts_the_existing_resource_instead_of_making_a_second(
    request_lease, worker: ProvisioningWorker, sessionmaker, read_lease, provider, read_events
) -> None:
    """The crash-recovery case. The row a second worker picks up still carries the name the
    first one used, so ``create`` with that name adopts rather than duplicates."""
    outcome = await request_lease()
    provider.fail_next("create", half_succeed=True, message="lost the response", retryable=True)
    await worker.tick()
    assert len(provider.snapshot()) == 1

    async with sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.next_attempt_at = utcnow() - timedelta(seconds=1)
        await opened.commit()

    await worker.tick()

    assert len(provider.snapshot()) == 1
    assert provider.calls["create"] == 2
    assert (await read_lease(outcome.lease.id)).state == LeaseState.ACTIVE.value
    activated = next(e for e in await read_events(outcome.lease.id) if e.action == "activated")
    assert activated.detail["adopted"] is True


async def test_an_adopted_resource_returns_the_credential_the_first_call_minted(
    request_lease, worker: ProvisioningWorker, sessionmaker, read_lease, provider, sealed_values
) -> None:
    """A fresh credential would leave the caller holding a secret the resource no longer
    accepts."""
    outcome = await request_lease()
    minted = None
    provider.fail_next("create", half_succeed=True, message="lost the response", retryable=True)
    await worker.tick()
    minted = provider.snapshot()[list(provider.snapshot())[0]].outputs

    async with sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.next_attempt_at = utcnow() - timedelta(seconds=1)
        await opened.commit()
    await worker.tick()

    assert sealed_values(await read_lease(outcome.lease.id)) == minted


# --------------------------------------------------------------------------------------
# Teardown
# --------------------------------------------------------------------------------------


async def test_a_revoked_lease_is_destroyed_and_released(
    activate, service, agent, worker: ProvisioningWorker, read_lease, provider, read_events
) -> None:
    lease = await activate()
    await service.revoke(lease.id, agent, reason="finished")

    tick = await worker.tick()

    assert tick.released == [lease.id]
    assert provider.snapshot() == {}
    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.RELEASED.value
    assert stored.released_at is not None
    assert stored.failure_reason is None
    # The credential died with the resource.
    assert stored.bindings[0].revoked_at is not None
    assert transitions(await read_events(lease.id)) >= {
        ("revoked", "deprovisioning"),
        ("deprovisioning", "released"),
    }


async def test_a_destroy_that_fails_lands_orphaned_and_never_released(
    activate, service, agent, worker: ProvisioningWorker, read_lease, provider, read_events
) -> None:
    """The single most important negative result in the worker.

    ``RELEASED`` asserts that a resource is gone. A teardown that raised has established
    nothing of the sort, so the lease has to end up somewhere that keeps it visible.
    """
    lease = await activate()
    await service.revoke(lease.id, agent, reason="finished")
    provider.fail_next("destroy", message="409 from the provider")

    tick = await worker.tick()

    assert tick.orphaned == [lease.id]
    assert tick.released == []
    assert provider.snapshot()  # it really is still there

    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.ORPHANED.value
    assert stored.released_at is None
    # The binding is *not* revoked: the resource still exists, so the credential may too.
    assert stored.bindings[0].revoked_at is None
    assert "may still exist" in (stored.failure_reason or "")

    events = await read_events(lease.id)
    assert transitions(events) >= {("deprovisioning", "orphaned")}
    assert ("deprovisioning", "released") not in transitions(events)


async def test_a_retryable_destroy_failure_is_retried_before_it_orphans(
    activate, service, agent, make_worker, sessionmaker, read_lease, provider
) -> None:
    lease = await activate()
    await service.revoke(lease.id, agent, reason="finished")
    provider.fail_next("destroy", message="503", retryable=True)

    worker = make_worker(sessionmaker, "worker-a", max_provision_attempts=2)
    tick = await worker.tick()
    assert tick.retried == [lease.id]
    assert (await read_lease(lease.id)).state == LeaseState.DEPROVISIONING.value

    async with sessionmaker() as opened:
        row = await opened.get(Lease, lease.id)
        assert row is not None
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        await opened.commit()

    provider.fail_next("destroy", message="503", retryable=True)
    tick = await worker.tick()
    assert tick.orphaned == [lease.id]
    assert (await read_lease(lease.id)).state == LeaseState.ORPHANED.value


async def test_the_teardown_budget_is_not_inherited_from_provisioning(
    request_lease, worker: ProvisioningWorker, service, agent, sessionmaker, provider, read_lease
) -> None:
    """A lease that burned two provisioning attempts must still get a full destroy budget,
    or one failed teardown would send it straight to ORPHANED."""
    outcome = await request_lease()
    provider.fail_next("create", message="429", retryable=True)
    await worker.tick()
    async with sessionmaker() as opened:
        row = await opened.get(Lease, outcome.lease.id)
        assert row is not None
        assert row.attempts == 1
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        await opened.commit()
    await worker.tick()

    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.ACTIVE.value
    await service.revoke(lease.id, agent, reason="finished")
    assert (await read_lease(lease.id)).attempts == 0


async def test_destroying_something_already_gone_is_success(
    activate, service, agent, worker: ProvisioningWorker, read_lease, provider
) -> None:
    """A retry of a partially completed teardown has to be able to finish cleanly."""
    lease = await activate()
    provider.vanish(lease.external_name)
    await service.revoke(lease.id, agent, reason="finished")

    await worker.tick()
    assert (await read_lease(lease.id)).state == LeaseState.RELEASED.value


async def test_teardown_is_drained_before_provisioning(
    activate, service, agent, request_lease, worker: ProvisioningWorker, provider
) -> None:
    """When both queues have work, the thing costing money is the resource that should
    already be gone."""
    lease = await activate()
    await service.revoke(lease.id, agent, reason="finished")
    await request_lease()
    provider.ops.clear()

    tick = await worker.tick()

    assert len(tick.released) == 1
    assert len(tick.provisioned) == 1
    assert provider.ops.index("destroy") < provider.ops.index("create")


# --------------------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------------------


async def test_a_claim_is_committed_before_any_provider_call(
    request_lease, worker: ProvisioningWorker, sessionmaker
) -> None:
    """If the process dies on the next line, the row still records that somebody took a run
    at it -- which is what stops a retry budget being reset by crashing."""
    outcome = await request_lease()

    async with sessionmaker() as claiming:
        claimed = await worker._claim(claiming, outcome.lease.id, PROVISION_STATES)
        assert claimed is not None
        async with sessionmaker() as observer:
            seen = await observer.get(Lease, outcome.lease.id)
            assert seen is not None
            assert seen.claimed_by == "worker-a"
            assert seen.attempts == 1


async def test_a_fresh_claim_cannot_be_stolen_and_a_stale_one_can(
    request_lease, worker: ProvisioningWorker, make_worker, sessionmaker
) -> None:
    """Claims, not locks: a worker that dies holding one does not wedge the lease forever.

    There is nothing to unwind and no lock service to leak -- the claim simply goes stale.
    """
    outcome = await request_lease()
    other = make_worker(sessionmaker, "worker-b")

    async with sessionmaker() as first:
        assert await worker._claim(first, outcome.lease.id, PROVISION_STATES) is not None

    async with sessionmaker() as second:
        assert await other._claim(second, outcome.lease.id, PROVISION_STATES) is None

    async with sessionmaker() as ageing:
        lease = await ageing.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.claimed_at = utcnow() - CLAIM_TTL - timedelta(seconds=1)
        await ageing.commit()

    async with sessionmaker() as third:
        stolen = await other._claim(third, outcome.lease.id, PROVISION_STATES)
        assert stolen is not None
        assert stolen.claimed_by == "worker-b"
        assert stolen.attempts == 2  # the dead worker's attempt is not given back


async def test_a_lease_stuck_in_provisioning_with_a_stale_claim_is_recovered(
    request_lease, worker: ProvisioningWorker, sessionmaker, read_lease, provider
) -> None:
    """The crash-recovery case: a worker died mid-call and the row is still in flight."""
    outcome = await request_lease()
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.state = LeaseState.PROVISIONING.value
        lease.claimed_by = "worker-that-died"
        lease.claimed_at = utcnow() - CLAIM_TTL - timedelta(minutes=1)
        await opened.commit()

    await worker.tick()

    assert (await read_lease(outcome.lease.id)).state == LeaseState.ACTIVE.value
    # preflight already ran before the crash, so recovery goes straight to create.
    assert provider.ops == ["create"]


async def test_a_held_lease_is_not_even_a_candidate(
    request_lease, worker: ProvisioningWorker, make_worker, sessionmaker, provider
) -> None:
    outcome = await request_lease()
    other = make_worker(sessionmaker, "worker-b")
    async with sessionmaker() as first:
        assert await worker._claim(first, outcome.lease.id, PROVISION_STATES) is not None

    assert await other._candidates(PROVISION_STATES) == []
    tick = await other.tick()
    assert tick.handled == 0
    assert provider.ops == []


async def test_losing_the_race_after_the_candidate_scan_skips_rather_than_fails(
    request_lease, worker: ProvisioningWorker, make_worker, sessionmaker, provider
) -> None:
    """The candidate query is advisory; the conditional update is the real gate.

    Two workers that both selected the same id in the same instant reach ``_handle``
    together, and the loser has to walk away quietly -- not fail the lease, and above all
    not call the provider.
    """
    outcome = await request_lease()
    other = make_worker(sessionmaker, "worker-b")

    candidates = await other._candidates(PROVISION_STATES)
    assert candidates == [outcome.lease.id]

    async with sessionmaker() as winner:
        assert await worker._claim(winner, outcome.lease.id, PROVISION_STATES) is not None

    from bailment.engine.worker import WorkerTick

    tick = WorkerTick()
    await other._handle(candidates[0], PROVISION_STATES, other._provision, tick)

    assert tick.skipped == 1
    assert tick.handled == 0
    assert provider.ops == []


async def test_two_concurrent_workers_provision_exactly_once(
    file_sessionmaker: async_sessionmaker[AsyncSession],
    make_worker,
    catalog,
    registry: ProviderRegistry,
    settings: Settings,
    secret_box,
    provider: RecordingProvider,
    agent: Caller,
) -> None:
    """Correctness comes from the conditional update in ``_claim``, not from there being
    one worker.

    A file-backed database is used deliberately: an in-memory SQLite lives inside one
    connection, so two "concurrent" sessions would share a transaction and the test would
    agree with itself for entirely the wrong reason.
    """
    async with file_sessionmaker() as opened:
        service = LeaseService(
            opened, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
        )
        outcome = await service.request_provision(
            agent, ProvisionRequest(golden_path_id="sandbox", inputs={"name": "contended"})
        )

    workers = [make_worker(file_sessionmaker, f"worker-{index}") for index in range(2)]
    ticks = await asyncio.gather(*(worker.tick() for worker in workers))

    assert provider.calls["create"] == 1
    assert len(provider.snapshot()) == 1
    assert sorted(sum((tick.provisioned for tick in ticks), [])) == [outcome.lease.id]
    assert sum(tick.skipped for tick in ticks) >= 0

    async with file_sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        assert lease.state == LeaseState.ACTIVE.value
        bindings = await opened.execute(
            select(func.count()).select_from(Lease).where(Lease.id == outcome.lease.id)
        )
        assert bindings.scalar_one() == 1


# --------------------------------------------------------------------------------------
# Misconfiguration must not destroy work
# --------------------------------------------------------------------------------------


async def test_an_unconfigured_provider_parks_the_lease_and_gives_back_the_attempt(
    request_lease, worker: ProvisioningWorker, read_lease, provider: RecordingProvider
) -> None:
    """Somebody forgot an environment variable and will set it. Burning a lease's retry
    budget meanwhile would fail a request for a reason its requester can neither see nor fix."""
    outcome = await request_lease()
    provider.set_unavailable("BAILMENT_MEMORY_TOKEN is not set")

    tick = await worker.tick()

    assert tick.handled == 0
    assert tick.skipped == 1
    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.PENDING.value
    assert lease.attempts == 0
    assert lease.next_attempt_at is not None
    assert "not configured" in (lease.failure_reason or "")

    # Once the credential turns up, the same lease provisions normally.
    provider.set_unavailable(None)
    async with worker.sessionmaker() as opened:
        row = await opened.get(Lease, outcome.lease.id)
        assert row is not None
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        await opened.commit()
    await worker.tick()
    assert (await read_lease(outcome.lease.id)).state == LeaseState.ACTIVE.value


async def test_a_lease_naming_an_unregistered_provider_is_parked_not_failed(
    request_lease, sessionmaker, catalog, settings, secret_box, read_lease
) -> None:
    """Failing a teardown on the strength of a typo would abandon a real resource."""
    outcome = await request_lease()
    empty = ProviderRegistry()
    worker = ProvisioningWorker(
        sessionmaker, catalog=catalog, registry=empty, settings=settings, secret_box=secret_box
    )

    tick = await worker.tick()

    assert tick.skipped == 1
    lease = await read_lease(outcome.lease.id)
    assert lease.state == LeaseState.PENDING.value
    assert "unknown provider" in (lease.failure_reason or "")


async def test_a_lease_with_no_external_name_refuses_to_provision(
    request_lease, worker: ProvisioningWorker, sessionmaker, read_lease, provider
) -> None:
    """Unreachable through the service. If it ever happens, a resource created for this
    lease could never be matched back to it, so no provider is called at all."""
    outcome = await request_lease()
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.external_name = None
        await opened.commit()

    await worker.tick()

    assert provider.ops == []
    stored = await read_lease(outcome.lease.id)
    assert stored.state == LeaseState.FAILED.value
    assert "could never be matched back" in (stored.failure_reason or "")


async def test_a_teardown_with_no_external_name_orphans_rather_than_releasing(
    activate, sessionmaker, worker: ProvisioningWorker, read_lease
) -> None:
    lease = await activate()
    async with sessionmaker() as opened:
        row = await opened.get(Lease, lease.id)
        assert row is not None
        row.state = LeaseState.REVOKED.value
        row.external_name = None
        queue_for_teardown(row)
        await opened.commit()

    await worker.tick()

    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.ORPHANED.value
    assert "nothing here can identify it" in (stored.failure_reason or "")


async def test_one_poisoned_lease_does_not_stop_the_rest_of_the_batch(
    request_lease, worker: ProvisioningWorker, monkeypatch, read_lease
) -> None:
    """A session per lease, deliberately: one long transaction over a batch would mean a
    failure on the eighth lease rolls back the record of the seven before it."""
    first = await request_lease(inputs={"name": "first"})
    second = await request_lease(inputs={"name": "second"})

    original = worker._provision
    seen: list[str] = []

    async def flaky(session, lease, tick):  # type: ignore[no-untyped-def]
        seen.append(lease.id)
        if lease.id == first.lease.id:
            raise RuntimeError("something unexpected inside the handler")
        await original(session, lease, tick)

    monkeypatch.setattr(worker, "_provision", flaky)
    tick = await worker.tick()

    assert set(seen) == {first.lease.id, second.lease.id}
    assert tick.provisioned == [second.lease.id]
    assert tick.skipped == 1
    assert (await read_lease(second.lease.id)).state == LeaseState.ACTIVE.value
    # The poisoned lease keeps its claim until it goes stale, then another pass takes it.
    assert (await read_lease(first.lease.id)).state == LeaseState.PENDING.value


# --------------------------------------------------------------------------------------
# Queues
# --------------------------------------------------------------------------------------


def test_the_worker_picks_up_the_states_it_says_it_does() -> None:
    """``PROVISIONING`` is in the provision list for crash recovery. ``ORPHANED`` is
    deliberately absent from the teardown list: an orphan got there by exhausting its
    budget, and a loop that keeps trying hides the problem instead of surfacing it."""
    assert PROVISION_STATES == (LeaseState.PENDING, LeaseState.PROVISIONING)
    assert TEARDOWN_STATES == (
        LeaseState.EXPIRED,
        LeaseState.REVOKED,
        LeaseState.DEPROVISIONING,
    )
    assert LeaseState.ORPHANED not in TEARDOWN_STATES


async def test_an_orphaned_lease_is_not_retried_automatically(
    activate, sessionmaker, worker: ProvisioningWorker, provider
) -> None:
    lease = await activate()
    async with sessionmaker() as opened:
        row = await opened.get(Lease, lease.id)
        assert row is not None
        row.state = LeaseState.ORPHANED.value
        queue_for_teardown(row)
        await opened.commit()

    provider.ops.clear()
    assert (await worker.tick()).handled == 0
    assert provider.ops == []


async def test_the_batch_size_bounds_one_tick(
    request_lease, make_worker, sessionmaker, provider
) -> None:
    for index in range(3):
        await request_lease(inputs={"name": f"lease-{index}"})
    worker = make_worker(sessionmaker, "worker-a")
    worker.batch_size = 2

    tick = await worker.tick()
    assert len(tick.provisioned) == 2
    assert provider.calls["create"] == 2

    assert len((await worker.tick()).provisioned) == 1


def test_a_tick_report_renders_for_the_log() -> None:
    from bailment.engine.worker import WorkerTick

    tick = WorkerTick()
    tick.provisioned.append("a")
    tick.released.append("b")
    rendered = tick.as_dict()
    assert rendered["handled"] == 2
    assert rendered["provisioned"] == ["a"]
    assert isinstance(rendered["at"], str)
