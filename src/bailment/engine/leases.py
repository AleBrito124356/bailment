"""The lease ticker: warn, expire, renew, revoke, and time out unanswered approvals.

This is the module that makes the second of bailment's three promises true. A broker that
hands out time-boxed credentials but has nothing running the clock has handed out
permanent credentials with an optimistic comment attached.

**Warning is a separate state, not a notification side effect.** ``EXPIRING`` is a real
state a lease occupies, with ``warned_at`` recording when it got there. The alternative
-- fire a webhook and move on -- means that if the notification fails, or the process
restarts, nobody ever finds out the lease was about to end, and there is no way to look
at a row and tell whether anyone was told. A state plus a timestamp is idempotent: the
tick can run every fifteen seconds forever and the warning goes out once.

**Expiry is an intention, not a fact.** ``EXPIRED`` means the TTL elapsed. It does not
mean the resource is gone -- only a worker that called the provider and got a clean
answer may write ``RELEASED``. Keeping those separate is what makes a failed teardown
visible instead of indistinguishable from a successful one. Everything this module does
on the teardown side is therefore just queueing: set the state, clear the claim, and let
:mod:`bailment.engine.worker` do the part that can fail.

**An unanswered approval must expire, and must say so.** An approval queue where requests
sit forever is a queue nobody reads. When the deadline passes the lease is rejected --
but with a reason that says nobody answered, not a generic denial. The difference matters
to whoever reads it: "your request was denied" sends an agent looking for a policy it
violated, and there was none. It just needed a person and did not get one.

**The tick is idempotent and order-dependent.** Warn before expire, expire before queue,
so that a lease whose whole TTL elapsed between two ticks still passes through
``EXPIRING`` and still emits its notice. Skipping the warning for short leases would make
the sandbox path -- five minutes, one minute of warning -- silently different from every
other path, which is exactly the path people use to decide whether they trust this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.catalog.loader import Catalog
from bailment.catalog.schema import GoldenPath, format_duration
from bailment.config import Settings, get_settings
from bailment.engine.service import (
    ACTION_APPROVAL_EXPIRED,
    ACTION_EXPIRED,
    ACTION_QUEUED,
    ACTION_RENEWED,
    ACTION_REVOKED,
    ACTION_WARNED,
    aware,
    clamp_ttl,
    queue_for_teardown,
    record_event,
    seconds_remaining,
    transition,
)
from bailment.logging import get_logger
from bailment.models import Approval, Lease, utcnow
from bailment.states import TEARDOWN_INTENT_STATES, LeaseState

__all__ = [
    "LeaseNotice",
    "LeaseTicker",
    "Notifier",
    "RenewalError",
    "TickReport",
    "effective_warn_before",
    "log_notice",
    "renew_lease",
    "revoke_lease",
]

log = get_logger("bailment.engine.leases")

NoticeKind = Literal["expiring", "expired", "teardown_queued", "approval_expired"]


@dataclass(frozen=True, slots=True)
class LeaseNotice:
    """Something a human or an agent would want to be told about.

    Deliberately plain data with no credential-shaped fields on it, so that a deployment
    can hand it to Slack, to email or to a webhook without anybody having to audit what
    a notice contains first.
    """

    kind: NoticeKind
    lease_id: str
    golden_path_id: str
    requester: str
    on_behalf_of: str | None
    external_name: str | None
    expires_at: datetime | None
    seconds_remaining: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lease_id": self.lease_id,
            "golden_path": self.golden_path_id,
            "requester": self.requester,
            "on_behalf_of": self.on_behalf_of,
            "external_name": self.external_name,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "seconds_remaining": self.seconds_remaining,
            "message": self.message,
        }


Notifier = Callable[[LeaseNotice], Awaitable[None]]


async def log_notice(notice: LeaseNotice) -> None:
    """The default notifier. Structured log line, nothing else.

    A default that tried to reach a network would make the ticker's correctness depend on
    something outside the process, and a default that did nothing at all would make the
    expiry warning invisible in the one deployment shape everybody starts with.
    """
    log.info(
        "lease notice",
        kind=notice.kind,
        lease_id=notice.lease_id,
        golden_path=notice.golden_path_id,
        requester=notice.requester,
        external_name=notice.external_name,
        seconds_remaining=notice.seconds_remaining,
        message=notice.message,
    )


@dataclass(slots=True)
class TickReport:
    """What one pass of the ticker did. Returned so a test or the CLI can assert on it."""

    at: datetime = field(default_factory=utcnow)
    warned: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    queued_for_teardown: list[str] = field(default_factory=list)
    approvals_expired: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            len(self.warned)
            + len(self.expired)
            + len(self.queued_for_teardown)
            + len(self.approvals_expired)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "warned": list(self.warned),
            "expired": list(self.expired),
            "queued_for_teardown": list(self.queued_for_teardown),
            "approvals_expired": list(self.approvals_expired),
            "errors": list(self.errors),
            "changed": self.changed,
        }


class RenewalError(Exception):
    """A renewal was refused. The message is written for the requester."""


# --------------------------------------------------------------------------------------
# Pure lease arithmetic, shared with the service layer
# --------------------------------------------------------------------------------------


def effective_warn_before(path: GoldenPath, ttl_seconds: int) -> timedelta:
    """How long before expiry this particular lease should start warning.

    :class:`~bailment.catalog.schema.LeasePolicy` already refuses a ``warn_before`` that
    is longer than ``default_ttl``, but a caller may ask for a TTL shorter than the
    default -- the ceiling is clamped, the floor is only :data:`MIN_TTL_SECONDS` -- and a
    five minute warning on a two minute lease would put the lease into ``EXPIRING`` at
    the instant it was created. Capping the window at half the TTL means the warning is
    always a warning rather than a description of the present.
    """
    configured = path.lease.warn_before.delta
    ceiling = timedelta(seconds=max(1, ttl_seconds // 2))
    return min(configured, ceiling)


def renew_lease(
    session: AsyncSession,
    lease: Lease,
    path: GoldenPath,
    *,
    requested_seconds: int | None,
    actor: str,
) -> tuple[int, str | None]:
    """Extend a lease. Returns the granted TTL and a notice if it was clamped.

    Three rules, and the reasoning behind the third is the interesting one:

    * the golden path must allow renewal at all,
    * ``renewals`` must be below ``max_renewals``, so a lease cannot become permanent by
      attrition,
    * the new deadline is measured **from now**, not from the old deadline, and never
      moves earlier.

    From-now is what a requester means by "give me another four hours". Stacking onto the
    existing deadline would let three renewals of a four hour lease produce a sixteen
    hour lease while every individual number still looked like it respected ``max_ttl``.
    Never-earlier is the other half: renewing with a shorter TTL than the time already
    remaining must not cut the lease short, because nobody has ever meant that.
    """
    state = lease.lease_state
    if state not in (LeaseState.ACTIVE, LeaseState.EXPIRING):
        raise RenewalError(
            f"lease {lease.id} is {lease.state}; only an active or expiring lease can be "
            f"renewed. Request a new one."
        )
    if not path.lease.renewable:
        raise RenewalError(
            f"the '{path.id}' golden path does not allow renewal. Request a new lease, or "
            f"ask for up to {path.lease.max_ttl} up front."
        )
    if lease.renewals >= path.lease.max_renewals:
        raise RenewalError(
            f"lease {lease.id} has already been renewed {lease.renewals} time(s), which is "
            f"the limit for the '{path.id}' path. A resource that keeps needing extending "
            f"has stopped being temporary; ask a platform operator for a permanent one."
        )

    granted, notice = clamp_ttl(path, requested_seconds)
    now = utcnow()
    proposed = now + timedelta(seconds=granted)
    current = aware(lease.expires_at) if lease.expires_at is not None else None
    new_expiry = proposed if current is None or proposed > current else current

    previous_expiry = current
    lease.ttl_seconds = granted
    lease.expires_at = new_expiry
    lease.renewals += 1
    # Cleared so the lease warns again on its new deadline. Leaving it set is the bug
    # that makes a renewed lease expire without anybody being told a second time.
    lease.warned_at = None

    if state is LeaseState.EXPIRING:
        transition(
            session,
            lease,
            LeaseState.ACTIVE,
            actor=actor,
            action=ACTION_RENEWED,
            detail={
                "ttl_seconds": granted,
                "renewals": lease.renewals,
                "previous_expires_at": previous_expiry.isoformat() if previous_expiry else None,
                "expires_at": new_expiry.isoformat(),
            },
        )
    else:
        record_event(
            session,
            lease,
            actor=actor,
            action=ACTION_RENEWED,
            from_state=lease.state,
            to_state=lease.state,
            detail={
                "ttl_seconds": granted,
                "renewals": lease.renewals,
                "previous_expires_at": previous_expiry.isoformat() if previous_expiry else None,
                "expires_at": new_expiry.isoformat(),
            },
        )
        lease.updated_at = now

    remaining = path.lease.max_renewals - lease.renewals
    tail = (
        f" {remaining} renewal(s) left."
        if remaining > 0
        else " That was the last renewal this path allows."
    )
    return granted, (notice + tail if notice else None)


def revoke_lease(session: AsyncSession, lease: Lease, *, actor: str, reason: str) -> LeaseState:
    """End a lease early and queue whatever it created for destruction.

    A lease that never reached a provider is *rejected* rather than revoked. ``REVOKED``
    is an instruction to a teardown worker, and pointing one at a request that was still
    waiting for a human would put a row in the destroy queue with no resource behind it.
    The state machine agrees: ``PENDING`` and ``AWAITING_APPROVAL`` have no edge to
    ``REVOKED``.
    """
    state = lease.lease_state
    if state in (LeaseState.PENDING, LeaseState.AWAITING_APPROVAL):
        lease.failure_reason = reason
        lease.next_attempt_at = None
        transition(
            session,
            lease,
            LeaseState.REJECTED,
            actor=actor,
            action=ACTION_REVOKED,
            detail={"reason": reason, "note": "cancelled before anything was provisioned"},
        )
        return LeaseState.REJECTED

    transition(
        session,
        lease,
        LeaseState.REVOKED,
        actor=actor,
        action=ACTION_REVOKED,
        detail={"reason": reason},
    )
    queue_for_teardown(lease)
    return LeaseState.REVOKED


# --------------------------------------------------------------------------------------
# The ticker
# --------------------------------------------------------------------------------------

#: How many rows one tick will touch per category. A ceiling exists so that a first tick
#: against a database with ten thousand overdue leases does not hold one transaction open
#: for minutes; the next tick picks up the rest a few seconds later.
DEFAULT_BATCH: Final = 200


class LeaseTicker:
    """Runs the lease clock.

    One instance per process. It owns its own sessions rather than borrowing one, because
    a tick is a background unit of work with its own commit boundary and nothing else
    should be able to roll it back.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        catalog: Catalog,
        settings: Settings | None = None,
        notifier: Notifier = log_notice,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.catalog = catalog
        self.settings = settings or get_settings()
        self.notifier = notifier
        self.batch_size = batch_size

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Tick until ``stop`` is set. Never exits because of one bad tick.

        A ticker that dies on an exception is a ticker that stops expiring leases, which
        is the failure this whole component exists to prevent. Errors are logged with a
        traceback and the loop continues; the same rows are still overdue next time.
        """
        stop = stop or asyncio.Event()
        interval = float(self.settings.lease_tick_seconds)
        log.info("lease ticker started", interval_seconds=interval)
        while not stop.is_set():
            try:
                report = await self.tick()
                if report.changed:
                    log.info("lease tick", **report.as_dict())
            except Exception:
                log.exception("lease tick failed; continuing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
        log.info("lease ticker stopped")

    async def tick(self, *, now: datetime | None = None) -> TickReport:
        """One pass. Warn, expire, queue teardown, time out approvals -- in that order."""
        moment = now or utcnow()
        report = TickReport(at=moment)
        notices: list[LeaseNotice] = []

        async with self.sessionmaker() as session:
            try:
                notices += await self._warn(session, moment, report)
                notices += await self._expire(session, moment, report)
                notices += await self._queue_teardown(session, report)
                notices += await self._expire_approvals(session, moment, report)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

        # Notifications go out only after the transaction that justified them committed.
        # The other order produces the worst possible message: "your lease is expiring",
        # sent about a row that was rolled back and is not expiring at all.
        for notice in notices:
            try:
                await self.notifier(notice)
            except Exception:
                log.exception("notifier failed", lease_id=notice.lease_id, kind=notice.kind)
                report.errors.append(f"notify {notice.lease_id}: notifier raised")
        return report

    # -- steps -------------------------------------------------------------------------

    async def _warn(
        self, session: AsyncSession, now: datetime, report: TickReport
    ) -> list[LeaseNotice]:
        """ACTIVE leases inside their warning window move to EXPIRING and emit a notice."""
        leases = await self._select(
            session,
            [LeaseState.ACTIVE],
            extra_null_check=True,
        )
        notices: list[LeaseNotice] = []
        for lease in leases:
            if lease.expires_at is None:
                continue
            path = self.catalog.find(lease.golden_path_id)
            window = (
                effective_warn_before(path, lease.ttl_seconds)
                if path is not None
                # A lease whose path has left the catalog still deserves a warning; half
                # the remaining TTL is the honest fallback when nobody declares one.
                else timedelta(seconds=max(1, lease.ttl_seconds // 2))
            )
            expires = aware(lease.expires_at)
            if expires - now > window:
                continue
            remaining = seconds_remaining(lease, now=now)
            lease.warned_at = now
            transition(
                session,
                lease,
                LeaseState.EXPIRING,
                actor="bailment",
                action=ACTION_WARNED,
                detail={
                    "expires_at": expires.isoformat(),
                    "seconds_remaining": remaining,
                    "warn_before": format_duration(window),
                },
            )
            report.warned.append(lease.id)
            notices.append(
                _notice(
                    "expiring",
                    lease,
                    remaining,
                    f"lease {lease.id} ({lease.golden_path_id}) expires in "
                    f"{format_duration(timedelta(seconds=max(0, remaining or 0)))}. "
                    f"Renew it if you still need it; when it expires the resource is "
                    f"destroyed.",
                )
            )
        return notices

    async def _expire(
        self, session: AsyncSession, now: datetime, report: TickReport
    ) -> list[LeaseNotice]:
        """ACTIVE and EXPIRING leases past their deadline become EXPIRED.

        ACTIVE is included, not only EXPIRING, because a lease whose entire remaining TTL
        elapsed between two ticks never passed through the warning window. The warn step
        runs first and will have moved it, but a lease created with a TTL shorter than
        one tick can still land here directly, and refusing to expire it because it never
        got a warning would be the wrong way round.
        """
        leases = await self._select(session, [LeaseState.ACTIVE, LeaseState.EXPIRING])
        notices: list[LeaseNotice] = []
        for lease in leases:
            if lease.expires_at is None or aware(lease.expires_at) > now:
                continue
            transition(
                session,
                lease,
                LeaseState.EXPIRED,
                actor="bailment",
                action=ACTION_EXPIRED,
                detail={
                    "expires_at": aware(lease.expires_at).isoformat(),
                    "note": "TTL elapsed; the resource has not been destroyed yet",
                },
            )
            queue_for_teardown(lease)
            report.expired.append(lease.id)
            report.queued_for_teardown.append(lease.id)
            notices.append(
                _notice(
                    "expired",
                    lease,
                    0,
                    f"lease {lease.id} ({lease.golden_path_id}) has expired and is queued "
                    f"for teardown. The credentials it produced stop working as soon as "
                    f"the resource is destroyed.",
                )
            )
        return notices

    async def _queue_teardown(self, session: AsyncSession, report: TickReport) -> list[LeaseNotice]:
        """Pick up EXPIRED or REVOKED leases that nothing has queued.

        Belt and braces. :func:`revoke_lease` and :meth:`_expire` both queue as they go,
        so a row reaching here means a process died between writing the state and writing
        the schedule -- exactly the crash this system claims to survive. Without this
        step such a lease would sit in ``EXPIRED`` forever with a live resource behind it.
        """
        result = await session.execute(
            select(Lease)
            .where(
                Lease.state.in_([s.value for s in TEARDOWN_INTENT_STATES]),
                Lease.next_attempt_at.is_(None),
                Lease.claimed_by.is_(None),
            )
            .limit(self.batch_size)
        )
        notices: list[LeaseNotice] = []
        for lease in result.scalars().all():
            queue_for_teardown(lease)
            record_event(
                session,
                lease,
                actor="bailment",
                action=ACTION_QUEUED,
                from_state=lease.state,
                to_state=lease.state,
                detail={"reason": "found in a teardown state with nothing scheduled"},
            )
            report.queued_for_teardown.append(lease.id)
            notices.append(
                _notice(
                    "teardown_queued",
                    lease,
                    seconds_remaining(lease),
                    f"lease {lease.id} was {lease.state} with no teardown scheduled and "
                    f"has been re-queued.",
                )
            )
            log.warning(
                "re-queued an unscheduled teardown",
                lease_id=lease.id,
                state=lease.state,
                external_name=lease.external_name,
            )
        return notices

    async def _expire_approvals(
        self, session: AsyncSession, now: datetime, report: TickReport
    ) -> list[LeaseNotice]:
        """Reject approval requests nobody answered, saying that is what happened."""
        result = await session.execute(
            select(Approval, Lease)
            .join(Lease, Lease.id == Approval.lease_id)
            .where(
                Approval.decided_at.is_(None),
                Approval.deadline_at.is_not(None),
                Lease.state == LeaseState.AWAITING_APPROVAL.value,
            )
            .limit(self.batch_size)
        )
        notices: list[LeaseNotice] = []
        for approval, lease in result.all():
            if approval.deadline_at is None or aware(approval.deadline_at) > now:
                continue
            waited = now - aware(approval.requested_at)
            reason = (
                f"Nobody answered this approval request. It was raised "
                f"{format_duration(timedelta(seconds=int(waited.total_seconds())))} ago and "
                f"the approval window closed at {aware(approval.deadline_at).isoformat()}. "
                f"This is not a policy denial -- the request was never refused, it simply "
                f"ran out of time waiting for a human"
                + (
                    f" ({', '.join(approval.allowed_approvers)} could have decided it)."
                    if approval.allowed_approvers
                    else " (any operator could have decided it)."
                )
                + " Ask again, and say what you need it for."
            )
            approval.decided_at = now
            approval.decided_by = "bailment:approval-deadline"
            approval.approved = False
            approval.decision_note = reason

            lease.failure_reason = reason
            lease.next_attempt_at = None
            transition(
                session,
                lease,
                LeaseState.REJECTED,
                actor="bailment",
                action=ACTION_APPROVAL_EXPIRED,
                detail={
                    "reason": reason,
                    "deadline_at": aware(approval.deadline_at).isoformat(),
                    "waited_seconds": int(waited.total_seconds()),
                    "allowed_approvers": list(approval.allowed_approvers or ()),
                },
            )
            report.approvals_expired.append(lease.id)
            notices.append(_notice("approval_expired", lease, None, reason))
            log.warning(
                "approval request timed out",
                lease_id=lease.id,
                golden_path=lease.golden_path_id,
                requester=lease.requester,
                waited_seconds=int(waited.total_seconds()),
            )
        return notices

    # -- internals ---------------------------------------------------------------------

    async def _select(
        self,
        session: AsyncSession,
        states: Sequence[LeaseState],
        *,
        extra_null_check: bool = False,
    ) -> list[Lease]:
        """Candidate leases in the given states, oldest deadline first.

        The deadline comparison is done in Python rather than in SQL, because the warning
        window is per-golden-path and the expression would have to join against something
        that does not exist in the database. The ``expires_at`` index still narrows the
        scan, and the batch ceiling bounds it.
        """
        query = (
            select(Lease)
            .where(
                Lease.state.in_([s.value for s in states]),
                Lease.expires_at.is_not(None),
            )
            .order_by(Lease.expires_at.asc())
            .limit(self.batch_size)
        )
        if extra_null_check:
            # Only leases that have not been warned yet; the state change alone would be
            # enough, but the timestamp makes a re-warn after a renewal explicit.
            query = query.where(Lease.warned_at.is_(None))
        result = await session.execute(query)
        return list(result.scalars().all())


def _notice(kind: NoticeKind, lease: Lease, remaining: int | None, message: str) -> LeaseNotice:
    return LeaseNotice(
        kind=kind,
        lease_id=lease.id,
        golden_path_id=lease.golden_path_id,
        requester=lease.requester,
        on_behalf_of=lease.on_behalf_of,
        external_name=lease.external_name,
        expires_at=aware(lease.expires_at) if lease.expires_at else None,
        seconds_remaining=remaining,
        message=message,
    )
