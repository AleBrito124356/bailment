"""The lease clock: warn, expire, renew, revoke, and time out unanswered approvals.

A broker that hands out time-boxed credentials but has nothing running the clock has
handed out permanent credentials with an optimistic comment attached, so the central test
here is that a warning fires exactly once and an expiry actually queues a teardown.

``warned_at`` earns its own assertions. The alternative design -- fire a webhook and move
on -- means that if the notification fails, or the process restarts, nobody ever finds out
the lease was about to end and there is no way to look at a row and tell whether anyone
was told. A state plus a timestamp is idempotent, and the "twice" tests are what prove it.

Time is moved by writing older timestamps rather than by patching the clock. A ticker that
only works when ``utcnow`` is a mock is a ticker whose SQLite timezone handling is
untested, and that handling -- see :func:`bailment.engine.service.aware` -- is exactly the
thing that breaks on SQLite and not on Postgres.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bailment.engine.leases import (
    LeaseTicker,
    effective_warn_before,
    renew_lease,
    revoke_lease,
)
from bailment.engine.service import ConflictingState, RenewalRefused
from bailment.models import Approval, Lease, utcnow
from bailment.states import LeaseState
from conftest import SEALED_VALUE_PREFIX, make_catalog, make_path


async def set_deadline(sessionmaker, lease_id: str, *, seconds_from_now: float) -> None:
    """Move a lease's deadline without touching anything else about it."""
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = utcnow() + timedelta(seconds=seconds_from_now)
        await opened.commit()


# --------------------------------------------------------------------------------------
# Warning
# --------------------------------------------------------------------------------------


async def test_a_lease_inside_its_warning_window_moves_to_expiring_once_and_only_once(
    activate, sessionmaker, ticker: LeaseTicker, read_lease, read_events, notices
) -> None:
    """The idempotence is the point: the tick runs every fifteen seconds forever."""
    lease = await activate()  # sandbox: 5m TTL, 1m warning
    await set_deadline(sessionmaker, lease.id, seconds_from_now=30)

    first = await ticker.tick()
    assert first.warned == [lease.id]

    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.EXPIRING.value
    assert stored.warned_at is not None

    second = await ticker.tick()
    third = await ticker.tick()
    assert second.warned == []
    assert third.warned == []

    warnings = [e for e in await read_events(lease.id) if e.action == "expiring"]
    assert len(warnings) == 1
    assert len([n for n in notices if n.kind == "expiring"]) == 1


async def test_a_lease_outside_its_warning_window_is_left_alone(
    activate, sessionmaker, ticker: LeaseTicker, read_lease
) -> None:
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=200)

    assert (await ticker.tick()).warned == []
    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.ACTIVE.value
    assert stored.warned_at is None


async def test_the_warning_notice_says_how_long_is_left_and_what_to_do(
    activate, sessionmaker, ticker: LeaseTicker, notices
) -> None:
    """The consumer is frequently a model deciding whether to start something long."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=30)
    await ticker.tick()

    notice = next(n for n in notices if n.kind == "expiring")
    assert notice.lease_id == lease.id
    assert notice.requester == "agent-one"
    assert notice.on_behalf_of == "dana"
    assert notice.external_name == lease.external_name
    assert 0 < (notice.seconds_remaining or 0) <= 30
    assert "Renew it if you still need it" in notice.message
    # A notice is handed to Slack, email or a webhook, so nothing credential-shaped is on it.
    assert SEALED_VALUE_PREFIX not in str(notice.as_dict())


def test_the_warning_window_is_capped_at_half_the_lease() -> None:
    """A five minute warning on a two minute lease would put the lease into EXPIRING at
    the instant it was created, which is a description of the present, not a warning."""
    path = make_path("windowed", default_ttl="1h", max_ttl="4h", warn_before="30m")
    assert effective_warn_before(path, 3600) == timedelta(minutes=30)
    assert effective_warn_before(path, 600) == timedelta(minutes=5)
    assert effective_warn_before(path, 60) == timedelta(seconds=30)
    assert effective_warn_before(path, 1) == timedelta(seconds=1)


async def test_a_lease_whose_path_has_left_the_catalog_still_gets_a_warning(
    activate, sessionmaker, catalog, settings, read_lease
) -> None:
    """Half the remaining TTL is the honest fallback when nobody declares a window."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=100)
    orphaned_catalog = make_catalog(make_path("something-else"))
    ticker = LeaseTicker(sessionmaker, catalog=orphaned_catalog, settings=settings)

    assert (await ticker.tick()).warned == [lease.id]
    assert (await read_lease(lease.id)).state == LeaseState.EXPIRING.value


# --------------------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------------------


async def test_an_overdue_lease_expires_and_is_queued_for_teardown(
    activate, sessionmaker, ticker: LeaseTicker, read_lease, read_events, notices
) -> None:
    """``EXPIRED`` is an intention, not a fact. Everything the ticker does on the teardown
    side is queueing; the part that can fail belongs to a worker."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-1)

    report = await ticker.tick()

    assert report.expired == [lease.id]
    assert report.queued_for_teardown == [lease.id]

    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.EXPIRED.value
    assert stored.released_at is None  # nothing has been destroyed yet
    assert stored.next_attempt_at is not None
    assert stored.claimed_by is None
    assert stored.attempts == 0
    assert stored.bindings[0].revoked_at is None

    assert "expired" in [e.action for e in await read_events(lease.id)]
    assert [n.kind for n in notices if n.kind == "expired"] == ["expired"]


async def test_an_expired_lease_is_then_torn_down_by_a_worker(
    activate, sessionmaker, ticker: LeaseTicker, worker, read_lease, provider
) -> None:
    """The two halves, together: the ticker sets the intention and the worker makes it true."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-1)

    await ticker.tick()
    await worker.tick()

    assert provider.snapshot() == {}
    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.RELEASED.value
    assert stored.bindings[0].revoked_at is not None


async def test_a_lease_whose_whole_ttl_elapsed_between_two_ticks_still_warns_first(
    activate, sessionmaker, ticker: LeaseTicker, read_events, notices
) -> None:
    """Warn before expire, in one pass. Skipping the warning for short leases would make
    the sandbox path silently different from every other one -- and that is the path people
    use to decide whether they trust this."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-5)

    report = await ticker.tick()

    assert report.warned == [lease.id]
    assert report.expired == [lease.id]
    actions = [e.action for e in await read_events(lease.id)]
    assert actions.index("expiring") < actions.index("expired")
    assert [n.kind for n in notices] == ["expiring", "expired"]


async def test_expiry_is_not_repeated(
    activate, sessionmaker, ticker: LeaseTicker, read_events
) -> None:
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-1)

    await ticker.tick()
    second = await ticker.tick()

    assert second.expired == []
    assert len([e for e in await read_events(lease.id) if e.action == "expired"]) == 1


async def test_a_teardown_state_with_nothing_scheduled_is_re_queued(
    activate, sessionmaker, ticker: LeaseTicker, read_lease, notices
) -> None:
    """Belt and braces. A row reaching here means a process died between writing the state
    and writing the schedule -- exactly the crash this system claims to survive. Without
    this step the lease would sit in EXPIRED forever with a live resource behind it."""
    lease = await activate()
    async with sessionmaker() as opened:
        row = await opened.get(Lease, lease.id)
        assert row is not None
        row.state = LeaseState.EXPIRED.value
        row.next_attempt_at = None
        row.claimed_by = None
        await opened.commit()

    report = await ticker.tick()

    assert report.queued_for_teardown == [lease.id]
    assert (await read_lease(lease.id)).next_attempt_at is not None
    assert [n.kind for n in notices] == ["teardown_queued"]


async def test_a_lease_with_no_deadline_is_never_expired(
    request_lease, ticker: LeaseTicker, read_lease
) -> None:
    """A pending request has no clock; expiring it would tear down a resource that was
    never created."""
    outcome = await request_lease()
    report = await ticker.tick()
    assert report.changed == 0
    assert (await read_lease(outcome.lease.id)).state == LeaseState.PENDING.value


# --------------------------------------------------------------------------------------
# Notification ordering
# --------------------------------------------------------------------------------------


async def test_notices_are_sent_only_after_the_transaction_that_justified_them_committed(
    activate, sessionmaker, catalog, settings, read_lease
) -> None:
    """The other order produces the worst possible message: "your lease is expiring", sent
    about a row that was rolled back and is not expiring at all."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-1)

    observed: list[str] = []

    async def check_the_database(notice) -> None:  # type: ignore[no-untyped-def]
        stored = await read_lease(notice.lease_id)
        observed.append(stored.state)

    ticker = LeaseTicker(
        sessionmaker, catalog=catalog, settings=settings, notifier=check_the_database
    )
    await ticker.tick()

    assert observed == [LeaseState.EXPIRED.value, LeaseState.EXPIRED.value]


async def test_a_notifier_that_raises_is_recorded_and_does_not_break_the_tick(
    activate, sessionmaker, catalog, settings, read_lease
) -> None:
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-1)

    async def explode(notice) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("slack is down")

    ticker = LeaseTicker(sessionmaker, catalog=catalog, settings=settings, notifier=explode)
    report = await ticker.tick()

    assert report.expired == [lease.id]
    assert len(report.errors) == 2
    assert "notifier raised" in report.errors[0]
    # The state change stands: a failed notification must not un-expire a lease.
    assert (await read_lease(lease.id)).state == LeaseState.EXPIRED.value


# --------------------------------------------------------------------------------------
# Renewal
# --------------------------------------------------------------------------------------


async def test_renewal_respects_max_renewals(activate, service, agent, read_lease) -> None:
    """A lease that keeps needing extending has stopped being temporary."""
    lease = await activate()  # sandbox allows two renewals

    for expected in (1, 2):
        outcome = await service.renew(lease.id, agent, ttl="10m")
        assert outcome.lease.renewals == expected

    assert (await read_lease(lease.id)).renewals == 2
    with pytest.raises(RenewalRefused, match="already been renewed 2 time"):
        await service.renew(lease.id, agent, ttl="10m")


async def test_renewal_is_measured_from_now_and_never_moves_the_deadline_earlier(
    activate, sessionmaker, session: AsyncSession, catalog
) -> None:
    """From-now is what a requester means by "give me another hour". Stacking onto the
    existing deadline would let three renewals of a four hour lease produce sixteen hours
    while every individual number still looked like it respected ``max_ttl``.
    """
    lease = await activate()
    path = catalog.get("sandbox")

    row = await session.get(Lease, lease.id)
    assert row is not None
    row.expires_at = utcnow() + timedelta(minutes=50)
    await session.commit()

    # Shorter than what is already left: nobody has ever meant "cut my lease short".
    granted, _ = renew_lease(session, row, path, requested_seconds=600, actor="dana")
    await session.commit()
    assert granted == 600
    assert row.expires_at > utcnow() + timedelta(minutes=45)

    # Longer: measured from now, not added to the old deadline.
    renew_lease(session, row, path, requested_seconds=3600, actor="dana")
    await session.commit()
    assert row.expires_at < utcnow() + timedelta(minutes=61)


async def test_renewing_clears_the_warning_so_the_lease_warns_again(
    activate, sessionmaker, ticker: LeaseTicker, service, agent, read_lease, read_events
) -> None:
    """Leaving ``warned_at`` set is the bug that makes a renewed lease expire without
    anybody being told a second time."""
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=30)
    await ticker.tick()
    assert (await read_lease(lease.id)).state == LeaseState.EXPIRING.value

    outcome = await service.renew(lease.id, agent, ttl="5m")
    assert outcome.lease.state is LeaseState.ACTIVE
    stored = await read_lease(lease.id)
    assert stored.warned_at is None

    await set_deadline(sessionmaker, lease.id, seconds_from_now=30)
    assert (await ticker.tick()).warned == [lease.id]
    assert len([e for e in await read_events(lease.id) if e.action == "expiring"]) == 2


async def test_renewing_an_expiring_lease_puts_it_back_to_active(
    activate, sessionmaker, ticker: LeaseTicker, service, agent, read_events
) -> None:
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=30)
    await ticker.tick()

    await service.renew(lease.id, agent, ttl="5m")
    renewed = next(e for e in await read_events(lease.id) if e.action == "renewed")
    assert (renewed.from_state, renewed.to_state) == ("expiring", "active")


async def test_a_renewal_above_the_ceiling_is_clamped_and_says_how_many_are_left(
    activate, service, agent
) -> None:
    lease = await activate()
    outcome = await service.renew(lease.id, agent, ttl="48h")
    assert outcome.ttl_clamped is True
    assert outcome.lease.ttl_seconds == 3600
    notice = "\n".join(outcome.notices)
    assert "clamped" in notice
    assert "renewal(s) left" in notice


async def test_the_last_renewal_says_so(activate, service, agent) -> None:
    """An agent that is told "1 renewal left" plans differently from one told nothing."""
    lease = await activate()
    await service.renew(lease.id, agent, ttl="48h")
    outcome = await service.renew(lease.id, agent, ttl="48h")
    assert "That was the last renewal this path allows." in "\n".join(outcome.notices)


async def test_a_non_renewable_path_refuses(
    session: AsyncSession, registry, settings, secret_box, agent, sessionmaker
) -> None:
    from bailment.engine.service import LeaseService, ProvisionRequest
    from bailment.engine.worker import ProvisioningWorker

    catalog = make_catalog(make_path("fixed", renewable=False))
    service = LeaseService(
        session, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
    )
    outcome = await service.request_provision(
        agent, ProvisionRequest(golden_path_id="fixed", inputs={"name": "x"})
    )
    await ProvisioningWorker(
        sessionmaker, catalog=catalog, registry=registry, settings=settings, secret_box=secret_box
    ).tick()

    with pytest.raises(RenewalRefused, match="does not allow renewal"):
        await service.renew(outcome.lease.id, agent, ttl="10m")


async def test_only_a_live_lease_can_be_renewed(request_lease, service, agent) -> None:
    outcome = await request_lease()
    with pytest.raises(RenewalRefused, match="only an active or expiring lease"):
        await service.renew(outcome.lease.id, agent, ttl="10m")


async def test_renewing_a_lease_whose_path_has_gone_is_refused(
    activate, session: AsyncSession, registry, settings, secret_box, agent
) -> None:
    """There is no ceiling to renew against, so the honest answer is to let it expire."""
    from bailment.engine.service import LeaseService

    lease = await activate()
    service = LeaseService(
        session,
        catalog=make_catalog(make_path("something-else")),
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )
    with pytest.raises(ConflictingState, match="no longer in the catalog"):
        await service.renew(lease.id, agent, ttl="10m")


# --------------------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------------------


async def test_revoking_a_live_lease_sets_the_intention_and_queues_it(
    activate, session: AsyncSession, read_lease
) -> None:
    lease = await activate()
    row = await session.get(Lease, lease.id)
    assert row is not None

    assert revoke_lease(session, row, actor="dana", reason="finished") is LeaseState.REVOKED
    await session.commit()

    stored = await read_lease(lease.id)
    assert stored.state == LeaseState.REVOKED.value
    assert stored.next_attempt_at is not None
    assert stored.attempts == 0


@pytest.mark.parametrize("state", [LeaseState.PENDING, LeaseState.AWAITING_APPROVAL])
async def test_revoking_something_never_provisioned_rejects_it_instead(
    request_lease, session: AsyncSession, state: LeaseState, read_lease
) -> None:
    """Pointing a teardown worker at a request that was still waiting for a human would
    put a row in the destroy queue with no resource behind it."""
    outcome = await request_lease()
    row = await session.get(Lease, outcome.lease.id)
    assert row is not None
    row.state = state.value

    assert revoke_lease(session, row, actor="dana", reason="cancelled") is LeaseState.REJECTED
    await session.commit()

    stored = await read_lease(outcome.lease.id)
    assert stored.state == LeaseState.REJECTED.value
    assert stored.next_attempt_at is None
    assert stored.failure_reason == "cancelled"


# --------------------------------------------------------------------------------------
# Approval deadlines
# --------------------------------------------------------------------------------------


async def test_an_unanswered_approval_expires_and_says_that_is_what_happened(
    request_lease, sessionmaker, ticker: LeaseTicker, read_lease, read_events, notices
) -> None:
    """ "Your request was denied" sends an agent looking for a policy it violated, and there
    was none. It just needed a person and did not get one.
    """
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    async with sessionmaker() as opened:
        approval = (
            (await opened.execute(select(Approval).where(Approval.lease_id == outcome.lease.id)))
            .scalars()
            .one()
        )
        approval.requested_at = utcnow() - timedelta(hours=25)
        approval.deadline_at = utcnow() - timedelta(hours=1)
        await opened.commit()

    report = await ticker.tick()

    assert report.approvals_expired == [outcome.lease.id]
    stored = await read_lease(outcome.lease.id)
    assert stored.state == LeaseState.REJECTED.value
    assert stored.next_attempt_at is None
    assert stored.approval is not None
    assert stored.approval.approved is False
    assert stored.approval.decided_by == "bailment:approval-deadline"

    reason = stored.failure_reason or ""
    assert "Nobody answered" in reason
    assert "not a policy denial" in reason
    assert "any operator could have decided it" in reason
    assert "Ask again" in reason

    assert "approval_expired" in [e.action for e in await read_events(outcome.lease.id)]
    assert [n.kind for n in notices] == ["approval_expired"]


async def test_an_approval_still_inside_its_window_is_left_alone(
    request_lease, ticker: LeaseTicker, read_lease
) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    report = await ticker.tick()
    assert report.approvals_expired == []
    assert (await read_lease(outcome.lease.id)).state == LeaseState.AWAITING_APPROVAL.value


async def test_an_already_decided_approval_is_never_expired(
    request_lease, sessionmaker, ticker: LeaseTicker, service, operator, read_lease
) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    await service.approve(outcome.lease.id, operator)
    async with sessionmaker() as opened:
        approval = (
            (await opened.execute(select(Approval).where(Approval.lease_id == outcome.lease.id)))
            .scalars()
            .one()
        )
        approval.deadline_at = utcnow() - timedelta(hours=1)
        await opened.commit()

    assert (await ticker.tick()).approvals_expired == []
    assert (await read_lease(outcome.lease.id)).state == LeaseState.PROVISIONING.value


async def test_the_expiry_reason_names_the_approvers_who_could_have_answered(
    session: AsyncSession, sessionmaker, registry, settings, secret_box, agent, ticker
) -> None:
    from bailment.catalog.schema import PolicyRule
    from bailment.engine.service import LeaseService, ProvisionRequest

    path = make_path(
        "named",
        policy=[PolicyRule(effect="require_approval", reason="alice decides", approvers=["alice"])],
    )
    service = LeaseService(
        session,
        catalog=make_catalog(path),
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )
    outcome = await service.request_provision(
        agent, ProvisionRequest(golden_path_id="named", inputs={"name": "x"})
    )
    approval = (
        (await session.execute(select(Approval).where(Approval.lease_id == outcome.lease.id)))
        .scalars()
        .one()
    )
    approval.deadline_at = utcnow() - timedelta(hours=1)
    await session.commit()

    await ticker.tick()

    async with sessionmaker() as opened:
        lease = await opened.get(Lease, outcome.lease.id)
        assert lease is not None
        assert "alice could have decided it" in (lease.failure_reason or "")


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


async def test_a_tick_report_renders_for_the_log(
    activate, sessionmaker, ticker: LeaseTicker
) -> None:
    lease = await activate()
    await set_deadline(sessionmaker, lease.id, seconds_from_now=-1)
    rendered = (await ticker.tick()).as_dict()
    assert rendered["changed"] == 3  # warned, expired, queued
    assert rendered["expired"] == [lease.id]
    assert isinstance(rendered["at"], str)
