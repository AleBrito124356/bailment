"""Drift detection in both directions. The flagship tests.

Credential brokering is a solved problem. What no product does is answer *"is there
anything running right now that nobody is accounting for?"*, and these tests are the
evidence that bailment does.

The four rules the module is built on each get their own block below, and each one is a
rule about **not** acting:

* **Report, do not delete.** Two switches, and either alone does nothing.
* **Nothing younger than the grace period is ever flagged.** Otherwise the reconciler
  races its own workers and reports healthy provisioning as drift.
* **UNKNOWN changes nothing.** Not the state, not a timestamp, not an audit row.
* **Only bailment's own marker is ever touched.** A resource without the prefix is
  reported as an integration bug and left completely alone, under any configuration.

Ages are set by writing older timestamps rather than by shrinking the grace period,
because a test that passes only with a one-second grace has not exercised the arithmetic
a ten-minute one uses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bailment.engine.reconciler import DEFAULT_GRACE, Reconciler
from bailment.models import Lease, ReconcileRun, utcnow
from bailment.providers.base import ManagedResource, ResourceStatus
from bailment.providers.registry import ProviderRegistry
from bailment.states import LeaseState
from conftest import RecordingProvider

OLD = timedelta(hours=2)


def age_resource(provider: RecordingProvider, external_name: str, by: timedelta) -> None:
    """Backdate a resource at the fake provider, so it is outside the grace period."""
    provider.snapshot()  # assert the name exists before we reach into the store
    resource = provider._resources[external_name]
    resource.created_at = resource.created_at - by


async def age_row(sessionmaker, lease_id: str, by: timedelta) -> None:
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, lease_id)
        assert lease is not None
        lease.created_at = lease.created_at - by
        if lease.activated_at is not None:
            lease.activated_at = lease.activated_at - by
        await opened.commit()


class UnmanagedProvider(RecordingProvider):
    """A provider whose server-side filter has widened, as a new API version might.

    ``MemoryProvider.list_managed`` already applies the prefix filter, which is correct and
    means the reconciler never sees an unmanaged name from it. That makes it useless for
    testing the reconciler's *own* defence, so this one lies.
    """

    async def list_managed(self) -> list[ManagedResource]:
        managed = await super().list_managed()
        return [
            *managed,
            ManagedResource(
                external_name="handmade-production-db",
                provider_resource_id="mem_handmade",
                created_at=utcnow() - OLD,
            ),
        ]


class AgelessProvider(RecordingProvider):
    """A provider that cannot say when a resource was created."""

    async def list_managed(self) -> list[ManagedResource]:
        return [
            ManagedResource(
                external_name="bailment-ageless-00000000",
                provider_resource_id="mem_ageless",
                created_at=None,
            )
        ]


# --------------------------------------------------------------------------------------
# Direction 1: orphans
# --------------------------------------------------------------------------------------


async def test_an_orphan_is_found_and_reported_but_not_destroyed_by_default(
    make_reconciler, provider: RecordingProvider
) -> None:
    """A tool that deletes cloud resources on its first run is a tool nobody installs twice."""
    provider.plant_orphan("bailment-sandbox-deadbeef", note="left by a worker that died")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)

    summary = await make_reconciler().run()

    assert summary.orphans_found == 1
    assert summary.orphans_destroyed == 0
    assert summary.clean is False
    assert summary.providers_checked == ("memory",)
    assert summary.providers_skipped == ()

    orphan = summary.providers[0].orphans[0]
    assert orphan.external_name == "bailment-sandbox-deadbeef"
    assert orphan.provider_resource_id
    assert orphan.destroyed is False
    assert orphan.destroy_error is None
    assert orphan.lease_id is None
    assert "no lease in this database names this resource" in orphan.reason
    assert orphan.age_seconds is not None and orphan.age_seconds >= OLD.total_seconds()

    # And it is still there. That is the whole assertion.
    assert "bailment-sandbox-deadbeef" in provider.snapshot()


@pytest.mark.parametrize(
    ("auto_destroy", "allowlist"),
    [(True, ()), (False, ("memory",)), (False, ())],
    ids=["global-switch-only", "allowlist-only", "neither"],
)
async def test_one_switch_alone_never_destroys_anything(
    make_reconciler, provider: RecordingProvider, auto_destroy: bool, allowlist: tuple[str, ...]
) -> None:
    """Two switches means nobody arrives at a deletion by accident."""
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)

    reconciler = make_reconciler(auto_destroy=auto_destroy, destroy_orphans_for=allowlist)
    assert reconciler.may_destroy("memory") is False
    assert reconciler.armed_providers == ()

    summary = await reconciler.run()

    assert summary.orphans_found == 1
    assert summary.orphans_destroyed == 0
    assert "bailment-sandbox-deadbeef" in provider.snapshot()
    assert provider.calls["destroy"] == 0


async def test_both_switches_together_destroy_the_orphan(
    make_reconciler, provider: RecordingProvider
) -> None:
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)

    reconciler = make_reconciler(auto_destroy=True, destroy_orphans_for=["memory"])
    assert reconciler.may_destroy("memory") is True
    assert reconciler.armed_providers == ("memory",)

    summary = await reconciler.run()

    assert summary.orphans_found == 1
    assert summary.orphans_destroyed == 1
    assert summary.providers[0].destroy_armed is True
    assert summary.providers[0].orphans[0].destroyed is True
    assert provider.snapshot() == {}


async def test_a_destroy_that_fails_is_reported_rather_than_claimed(
    make_reconciler, provider: RecordingProvider
) -> None:
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)
    provider.fail_next("destroy", message="403 from the provider")

    summary = await make_reconciler(auto_destroy=True, destroy_orphans_for=["memory"]).run()

    orphan = summary.providers[0].orphans[0]
    assert orphan.destroyed is False
    assert "403 from the provider" in (orphan.destroy_error or "")
    assert summary.orphans_destroyed == 0
    assert "bailment-sandbox-deadbeef" in provider.snapshot()


async def test_an_orphan_of_unknown_age_is_never_destroyed_however_armed(
    make_reconciler, registry: ProviderRegistry
) -> None:
    """It may be thirty seconds old and mid-provision, and no amount of configuration
    should let the reconciler guess about that."""
    provider = AgelessProvider(latency_range=(0.0, 0.0))
    scoped = ProviderRegistry()
    scoped.register(provider)

    summary = await make_reconciler(
        auto_destroy=True, destroy_orphans_for=["memory"], provider_registry=scoped
    ).run()

    orphan = summary.providers[0].orphans[0]
    assert orphan.age_seconds is None
    assert orphan.destroyed is False
    assert provider.calls["destroy"] == 0


async def test_a_resource_inside_the_grace_window_is_never_flagged(
    make_reconciler, provider: RecordingProvider
) -> None:
    """Without this the reconciler races in-flight provisioning: a resource created ninety
    seconds ago by a worker that has not yet committed its result looks exactly like an
    orphan, and reporting it would mean reporting healthy work as drift."""
    provider.plant_orphan("bailment-sandbox-freshbee")  # created just now

    summary = await make_reconciler(auto_destroy=True, destroy_orphans_for=["memory"]).run()

    assert summary.orphans_found == 0
    assert summary.providers[0].within_grace == 1
    assert summary.providers[0].resources_seen == 1
    assert "bailment-sandbox-freshbee" in provider.snapshot()


async def test_a_resource_a_live_lease_accounts_for_is_not_an_orphan(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider
) -> None:
    lease = await activate()
    age_resource(provider, lease.external_name, OLD)
    await age_row(sessionmaker, lease.id, OLD)

    summary = await make_reconciler().run()

    assert summary.orphans_found == 0
    assert summary.drift_found == 0
    assert summary.clean is True
    assert summary.leases_checked == 1


async def test_a_resource_whose_lease_says_it_is_gone_is_an_orphan(
    activate, service, agent, worker, make_reconciler, sessionmaker, provider, read_lease
) -> None:
    """bailment believes this does not exist -- but the provider says it does.

    This is the failed-teardown case seen from the other side, and the reason the reason
    string names the lease state: the operator needs to know that the row and reality
    disagree, not merely that something is running.
    """
    lease = await activate()
    await service.revoke(lease.id, agent, reason="finished")
    provider.fail_next("destroy", message="403")
    await worker.tick()
    assert (await read_lease(lease.id)).state == LeaseState.ORPHANED.value

    # An ORPHANED lease is already accounted for and is counted separately, not re-flagged.
    await age_row(sessionmaker, lease.id, OLD)
    age_resource(provider, lease.external_name, OLD)
    summary = await make_reconciler().run()
    assert summary.orphans_found == 0
    assert summary.providers[0].known_orphan_leases == 1

    # Once a human marks it released without the resource actually going, it is an orphan.
    async with sessionmaker() as opened:
        row = await opened.get(Lease, lease.id)
        assert row is not None
        row.state = LeaseState.RELEASED.value
        await opened.commit()

    summary = await make_reconciler().run()
    orphan = summary.providers[0].orphans[0]
    assert orphan.lease_id == lease.id
    assert orphan.lease_state == "released"
    assert "the provider says it does" in orphan.reason


# --------------------------------------------------------------------------------------
# The marker
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("armed", [False, True], ids=["report-only", "armed"])
async def test_a_resource_without_bailment_s_marker_is_never_touched(
    make_reconciler, armed: bool
) -> None:
    """The blast radius of trusting a provider's filter is "delete a database a human made
    by hand", so the reconciler re-checks every name itself and refuses the ones that are
    not ours -- loudly, as an integration error, and under every configuration.
    """
    provider = UnmanagedProvider(latency_range=(0.0, 0.0))
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)
    scoped = ProviderRegistry()
    scoped.register(provider)

    summary = await make_reconciler(
        auto_destroy=armed,
        destroy_orphans_for=["memory"] if armed else (),
        provider_registry=scoped,
    ).run()

    result = summary.providers[0]
    assert result.resources_seen == 2
    # Only the one carrying the prefix is a finding.
    assert [orphan.external_name for orphan in result.orphans] == ["bailment-sandbox-deadbeef"]
    assert any("handmade-production-db" in error for error in result.errors)
    assert any("does not carry bailment's prefix" in error for error in result.errors)
    # Nothing was ever asked about it, let alone destroyed.
    assert "handmade-production-db" not in provider.snapshot()
    assert summary.clean is False


async def test_the_provider_itself_also_filters_unmanaged_names(
    provider: RecordingProvider,
) -> None:
    """Defence in depth: the reconciler's check is the second one, not the only one."""
    provider.plant_orphan("handmade-production-db")
    provider.plant_orphan("bailment-sandbox-deadbeef")
    listed = [resource.external_name for resource in await provider.list_managed()]
    assert listed == ["bailment-sandbox-deadbeef"]


# --------------------------------------------------------------------------------------
# Direction 2: vanished resources
# --------------------------------------------------------------------------------------


async def test_a_vanished_resource_marks_the_lease_released_with_an_audit_event(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider, read_lease, read_events
) -> None:
    """Deleted underneath us, usually by a person in a console.

    Left alone the lease keeps a name reserved, keeps a binding resolvable and keeps a slot
    in whatever quota policy counts. ``ACTIVE -> RELEASED`` is not a legal edge, so the
    reconciler says the two things it actually learned -- we no longer know the state of
    this resource, and then, we have established it is gone.
    """
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)
    assert provider.vanish(lease.external_name) is True

    summary = await make_reconciler().run()

    assert summary.drift_found == 1
    assert summary.orphans_found == 0
    assert summary.clean is False

    record = summary.providers[0].drift[0]
    assert record.lease_id == lease.id
    assert record.from_state == "active"
    assert record.to_state == "released"
    assert "destroyed outside bailment" in record.note

    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.RELEASED.value
    assert stored.released_at is not None
    assert stored.claimed_by is None
    assert stored.next_attempt_at is None
    # The credential is dead: the thing it opened is gone.
    assert stored.bindings[0].revoked_at is not None

    events = await read_events(lease.id)
    by_reconciler = [event for event in events if event.actor == "bailment:reconciler"]
    assert {(e.from_state, e.to_state) for e in by_reconciler} == {
        ("active", "unknown"),
        ("unknown", "released"),
    }
    assert {"state_unknown", "released"} == {event.action for event in by_reconciler}
    assert by_reconciler[0].detail["observed_by"] == "reconciler"


async def test_a_lease_that_never_activated_lands_on_failed_not_released(
    request_lease, make_reconciler, sessionmaker, read_lease
) -> None:
    """``RELEASED`` means "the resource this lease held is gone", which is only true of a
    lease that ever held one. Calling a never-delivered lease released would put it in the
    history books as having successfully served a resource it never produced.
    """
    outcome = await request_lease()
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        lease.state = LeaseState.PROVISIONING.value
        lease.created_at = lease.created_at - OLD
        await opened.commit()

    summary = await make_reconciler().run()

    assert summary.drift_found == 1
    assert summary.providers[0].drift[0].to_state == "failed"
    stored = await read_lease(outcome.lease.id)
    assert stored.state == LeaseState.FAILED.value
    assert stored.released_at is None
    assert "destroyed outside bailment" in (stored.failure_reason or "")


async def test_a_lease_a_worker_is_holding_right_now_is_left_alone(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider
) -> None:
    """Asking the provider mid-call and acting on the answer is how a reconciler races a
    create -- and manufactures the drift it exists to find."""
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)
    provider.vanish(lease.external_name)
    async with sessionmaker() as opened:
        row = await opened.get(Lease, lease.id)
        assert row is not None
        row.claimed_by = "worker-b"
        row.claimed_at = utcnow()
        await opened.commit()

    summary = await make_reconciler().run()

    assert summary.drift_found == 0
    assert summary.providers[0].in_flight_skipped == 1


async def test_a_lease_inside_the_grace_window_is_left_alone(
    activate, make_reconciler, provider: RecordingProvider
) -> None:
    lease = await activate()
    provider.vanish(lease.external_name)

    summary = await make_reconciler().run()

    assert summary.drift_found == 0
    assert summary.providers[0].within_grace >= 1


async def test_a_terminal_lease_is_not_asked_about(
    request_lease, make_reconciler, sessionmaker, provider: RecordingProvider
) -> None:
    outcome = await request_lease("forbidden", inputs={})
    await age_row(sessionmaker, outcome.lease.id, OLD)

    summary = await make_reconciler().run()

    assert summary.leases_checked == 0
    assert provider.calls["exists"] == 0


# --------------------------------------------------------------------------------------
# UNKNOWN
# --------------------------------------------------------------------------------------


async def test_an_unknown_status_changes_absolutely_nothing(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider, read_lease, read_events
) -> None:
    """The rule the whole module exists to obey.

    A provider that times out has told us nothing, and the one thing this module must
    never do is turn silence into ``RELEASED``. It is counted, so that a provider which
    answers UNKNOWN for a week is visible as a broken integration rather than as a clean
    account.
    """
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)
    provider.force_status(lease.external_name, ResourceStatus.UNKNOWN)

    before = await read_lease(lease.id)
    before_events = len(await read_events(lease.id))

    summary = await make_reconciler().run()

    assert summary.status_unknown == 1
    assert summary.providers[0].status_unknown == [lease.id]
    assert summary.drift_found == 0
    assert summary.orphans_found == 0
    # "Nothing found" is not the same as "nothing checked", and neither is clean.
    assert summary.clean is False

    after = await read_lease(lease.id)
    assert after.state == before.state
    assert after.updated_at == before.updated_at
    assert after.released_at is None
    assert after.failure_reason == before.failure_reason
    assert len(await read_events(lease.id)) == before_events


async def test_an_exists_call_that_raises_is_also_unknown(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider, read_lease
) -> None:
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)
    provider.fail_next("exists", message="504 gateway timeout")

    summary = await make_reconciler().run()

    assert summary.providers[0].status_unknown == [lease.id]
    assert any("504" in error for error in summary.providers[0].errors)
    assert (await read_lease(lease.id)).state == LeaseState.ACTIVE.value


async def test_an_enumeration_failure_does_not_read_as_a_clean_account(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider
) -> None:
    """An empty list would read as "the account is clean". It is not; we could not see it.

    The lease-by-lease direction still runs, because it does not depend on enumeration.
    """
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)
    provider.vanish(lease.external_name)
    provider.fail_next("list_managed", message="500 from the provider")

    summary = await make_reconciler().run()

    assert summary.providers[0].resources_seen == 0
    assert any("list_managed" in error for error in summary.providers[0].errors)
    assert summary.orphans_found == 0
    assert summary.drift_found == 1  # direction 2 still worked
    assert summary.clean is False


# --------------------------------------------------------------------------------------
# Phase 3: writing on stale reads
# --------------------------------------------------------------------------------------


async def test_a_row_a_worker_moved_mid_run_is_left_for_the_next_pass(
    activate, make_reconciler, sessionmaker, read_lease
) -> None:
    """Every write re-reads the row and re-checks that its state is still the one that was
    observed. Reconciling on stale reads is how a reconciler starts causing the drift it
    exists to find."""
    from bailment.engine.reconciler import ProviderReconcileResult, _Snapshot

    lease = await activate()
    snapshot = _Snapshot(
        id=lease.id,
        # The state the provider's answer was about. The row says ACTIVE, so a worker
        # moved it while the provider was being asked and the answer is about a row that
        # no longer exists in that form.
        state=LeaseState.EXPIRING,
        external_name=lease.external_name or "",
        provider_ref={},
        created_at=utcnow() - OLD,
        activated_at=utcnow() - OLD,
        claimed_at=None,
        claimed_by=None,
    )
    result = ProviderReconcileResult(provider="memory", started_at=utcnow())

    records = await make_reconciler()._apply_drift(
        "memory", [(snapshot, LeaseState.RELEASED)], result
    )

    assert records == []
    assert result.raced == 1
    assert (await read_lease(lease.id)).state == LeaseState.ACTIVE.value


# --------------------------------------------------------------------------------------
# Providers that cannot be swept
# --------------------------------------------------------------------------------------


async def test_an_unconfigured_provider_is_reported_as_skipped_not_as_clean(
    make_reconciler, provider: RecordingProvider, session: AsyncSession
) -> None:
    """A dashboard that only lists what worked cannot distinguish "no orphans at
    Cloudflare" from "Cloudflare was never asked"."""
    provider.set_unavailable("BAILMENT_MEMORY_TOKEN is not set")

    summary = await make_reconciler().run()

    assert summary.providers_checked == ()
    assert summary.providers_skipped == ("memory",)
    assert summary.clean is False
    assert "not configured" in (summary.providers[0].skipped_reason or "")

    # No run row is written, because a run row for a provider nobody reached would render
    # as a clean sweep in every orphans_found chart anybody ever builds off this table.
    rows = await session.execute(select(ReconcileRun))
    assert rows.scalars().all() == []


async def test_a_provider_that_cannot_enumerate_is_skipped_with_a_reason(
    make_reconciler, provider: RecordingProvider
) -> None:
    provider.supports_reconciliation = False

    summary = await make_reconciler().run()

    assert summary.providers_skipped == ("memory",)
    assert "cannot enumerate its own resources" in (summary.providers[0].skipped_reason or "")


async def test_an_unexpected_exception_from_a_provider_is_an_error_not_a_crash(
    activate, make_reconciler, sessionmaker, provider: RecordingProvider, monkeypatch
) -> None:
    """A provider is third-party code and may raise anything at all.

    It is recorded and the run continues, because the other direction and the other
    providers still have work to do -- but the run is not clean, so nobody reads it as an
    account with nothing wrong in it.
    """
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)

    async def explode() -> list[ManagedResource]:
        raise MemoryError("something unexpected")

    monkeypatch.setattr(provider, "list_managed", explode)

    summary = await make_reconciler().run()

    assert summary.providers_checked == ("memory",)
    assert summary.errors == 1
    assert "unexpected MemoryError" in summary.providers[0].errors[0]
    assert summary.clean is False
    # Direction 2 still ran against the same provider.
    assert summary.leases_checked == 1


async def test_a_failure_inside_the_reconciler_itself_is_reported_as_a_skip(
    make_reconciler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One provider blowing up must not stop the sweep of the others."""

    async def explode(_provider_name: str) -> list[Any]:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(Reconciler, "_snapshot", staticmethod(explode))

    summary = await make_reconciler().run()

    assert summary.providers_skipped == ("memory",)
    assert "RuntimeError" in (summary.providers[0].skipped_reason or "")
    assert summary.clean is False


# --------------------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------------------


async def test_every_checked_provider_writes_a_run_row(
    make_reconciler, provider: RecordingProvider, session: AsyncSession
) -> None:
    """The counters here are the project's headline metric: an installation whose teardown
    path works keeps ``orphans_found`` flat at zero."""
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)

    summary = await make_reconciler().run()

    rows = (await session.execute(select(ReconcileRun))).scalars().all()
    assert len(rows) == 1
    run = rows[0]
    assert run.id == summary.providers[0].run_id
    assert run.provider == "memory"
    assert run.orphans_found == 1
    assert run.orphans_destroyed == 0
    assert run.drift_found == 0
    assert run.resources_seen == 1
    assert run.finished_at is not None
    assert run.detail["destroy_armed"] is False
    assert run.detail["grace_seconds"] == int(DEFAULT_GRACE.total_seconds())
    assert run.detail["orphans"][0]["external_name"] == "bailment-sandbox-deadbeef"


async def test_a_clean_run_says_so(activate, make_reconciler, sessionmaker, provider) -> None:
    lease = await activate()
    await age_row(sessionmaker, lease.id, OLD)
    age_resource(provider, lease.external_name, OLD)

    summary = await make_reconciler().run()

    assert summary.clean is True
    assert summary.as_dict()["clean"] is True
    assert summary.finished_at is not None
    assert summary.resources_seen == 1
    assert summary.leases_checked == 1


async def test_the_summary_renders_for_a_dashboard(
    make_reconciler, provider: RecordingProvider
) -> None:
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)

    rendered = (await make_reconciler().run()).as_dict()

    assert rendered["orphans_found"] == 1
    assert rendered["providers_checked"] == ["memory"]
    assert rendered["providers"][0]["orphans"][0]["destroyed"] is False
    assert isinstance(rendered["started_at"], str)


async def test_reconciling_twice_reports_the_same_orphan_until_it_is_dealt_with(
    make_reconciler, provider: RecordingProvider
) -> None:
    """bailment keeps reporting it on every run, which is how it gets fixed."""
    provider.plant_orphan("bailment-sandbox-deadbeef")
    age_resource(provider, "bailment-sandbox-deadbeef", OLD)

    assert (await make_reconciler().run()).orphans_found == 1
    assert (await make_reconciler().run()).orphans_found == 1

    await provider.destroy(external_name="bailment-sandbox-deadbeef", provider_ref={})
    assert (await make_reconciler().run()).orphans_found == 0
