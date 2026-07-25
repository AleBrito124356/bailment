"""The worker that drains the provisioning and teardown queues.

Everything in this module is written for the case where it gets killed. A provisioning
worker holds the only live knowledge of an in-flight provider call, and the machine it
runs on will be rescheduled, OOM-killed or rebooted at some point during one. If that
leaves a resource nothing can find, bailment has failed at the only job it claims to be
good at.

**Claims, not locks.** A worker takes a row with a conditional ``UPDATE`` that only
succeeds if nobody currently holds it or the current holder's claim has gone stale. There
is no lock service, nothing to leak and nothing to unwind: a worker that dies holding a
claim simply stops refreshing it, and after :data:`CLAIM_TTL` another worker takes the
row. The claim is committed *before* any provider call, so the fact that somebody was
working on this lease survives the crash even though the work did not.

**The claim is what makes retry safe; the state is not.** The row a second worker picks
up still carries ``external_name``, written before the first worker called anything. That
name is the question the second worker asks the provider -- ``create`` with the same name
adopts rather than duplicates, ``exists`` answers about the right resource -- which is
why every provider is required to be idempotent under a supplied name and why bailment
never lets a provider name its own resources.

**The two failure paths are not symmetrical, on purpose.**

*Create failed.* Ask the provider whether anything is there, roll back what is, then
``FAILED``. If the rollback itself fails, or if the provider cannot say what exists, the
lease goes ``ORPHANED`` -- because something may be alive and billing, and the only wrong
answer is to stop looking.

*Destroy failed.* Never ``RELEASED``. Retry until the budget is gone, then ``ORPHANED``.
``RELEASED`` is a claim that the resource is gone, and a system that writes it
optimistically makes a failed teardown look exactly like a successful one -- at which
point the orphan is invisible forever and the reconciler has nothing to find.

**UNKNOWN is a stop on the way, not a destination.** The state machine has no edge from
``PROVISIONING`` to ``ORPHANED``, and that is not an oversight. Concluding "there is a
resource nobody owns" from a failed create requires first admitting "we do not know what
happened", so the worker transitions through ``UNKNOWN`` and both steps land in the audit
trail. The alternative would let one line of code turn a timeout into an assertion about
reality.

**A misconfigured deployment does not destroy work.** A provider that is registered but
has no credentials parks the lease and gives back the attempt it consumed. Burning a
lease's retry budget while an operator goes to fetch an API key would fail a request for
a reason its requester can neither see nor fix.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.catalog.loader import Catalog
from bailment.catalog.schema import GoldenPath
from bailment.config import Settings, get_settings
from bailment.engine.service import (
    ACTION_ACTIVATED,
    ACTION_CLAIMED,
    ACTION_ORPHANED,
    ACTION_PROVISION_FAILED,
    ACTION_PROVISIONING,
    ACTION_RELEASED,
    ACTION_ROLLED_BACK,
    ACTION_TEARDOWN,
    ACTION_UNKNOWN_STATE,
    backoff_seconds,
    record_event,
    transition,
)
from bailment.logging import bind_request, clear_request_context, get_logger
from bailment.models import Binding, Lease, new_id, utcnow
from bailment.providers.base import (
    Provider,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
)
from bailment.providers.registry import ProviderRegistry, UnknownProvider
from bailment.secrets import SecretBox, make_reference
from bailment.states import TRANSITIONS, LeaseState

__all__ = [
    "CLAIM_TTL",
    "PROVISION_STATES",
    "TEARDOWN_STATES",
    "ProvisioningWorker",
    "WorkerTick",
]

log = get_logger("bailment.engine.worker")

#: How long a claim is honoured before another worker may steal it.
#:
#: **This must match** :attr:`bailment.models.Lease.is_claim_stale`, which hard-codes the
#: same five minutes. That property is what a human reads when debugging; this constant
#: is what goes into the ``WHERE`` clause of the conditional update, and SQL cannot call
#: a Python property. If the two ever disagree, workers either steal each other's
#: in-flight leases (this value smaller) or a dead worker wedges a lease for longer than
#: the dashboard claims (this value larger).
CLAIM_TTL: Final = timedelta(minutes=5)

#: States a provisioning worker will pick up.
#:
#: ``PROVISIONING`` is in the list, and that is the crash-recovery case: a lease sitting
#: in ``PROVISIONING`` with a stale claim is one whose worker died mid-call, and it must
#: be picked up again rather than left as a permanent in-flight row.
PROVISION_STATES: Final[tuple[LeaseState, ...]] = (LeaseState.PENDING, LeaseState.PROVISIONING)

#: States a teardown worker will pick up. ``ORPHANED`` is deliberately absent: an orphan
#: got there by exhausting its budget against a provider that kept refusing, and a loop
#: that keeps trying anyway hides the problem instead of surfacing it. A human re-drives
#: it with :meth:`~bailment.engine.service.LeaseService.retry_teardown`.
TEARDOWN_STATES: Final[tuple[LeaseState, ...]] = (
    LeaseState.EXPIRED,
    LeaseState.REVOKED,
    LeaseState.DEPROVISIONING,
)


@dataclass(slots=True)
class WorkerTick:
    """What one pass of the worker did."""

    at: datetime = field(default_factory=utcnow)
    provisioned: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def handled(self) -> int:
        return (
            len(self.provisioned)
            + len(self.released)
            + len(self.failed)
            + len(self.orphaned)
            + len(self.retried)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "provisioned": list(self.provisioned),
            "released": list(self.released),
            "failed": list(self.failed),
            "orphaned": list(self.orphaned),
            "retried": list(self.retried),
            "skipped": self.skipped,
            "handled": self.handled,
        }


Handler = Callable[[AsyncSession, Lease, WorkerTick], Awaitable[None]]


class ProvisioningWorker:
    """Drains provisioning and teardown work.

    Run as many as you like against one database. Correctness comes from the conditional
    update in :meth:`_claim`, not from there being only one of these.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        catalog: Catalog,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        secret_box: SecretBox | None = None,
        batch_size: int = 10,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.catalog = catalog
        self.registry = registry
        self.settings = settings or get_settings()
        self.worker_id = self.settings.worker_id
        self.batch_size = max(1, batch_size)
        self._secret_box = secret_box

    @property
    def secret_box(self) -> SecretBox:
        """Built on first use so a teardown-only worker never needs an encryption key."""
        if self._secret_box is None:
            self._secret_box = SecretBox.from_settings(self.settings)
        return self._secret_box

    # -- loop --------------------------------------------------------------------------

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Work until ``stop`` is set, absorbing any exception a single tick raises.

        A worker that exits on an error stops tearing down expired resources, which is
        the failure this component exists to prevent. The rows are still there next tick.
        """
        stop = stop or asyncio.Event()
        interval = float(self.settings.lease_tick_seconds)
        log.info("provisioning worker started", worker_id=self.worker_id, interval=interval)
        while not stop.is_set():
            drained = False
            try:
                tick = await self.tick()
                if tick.handled:
                    log.info("worker tick", worker_id=self.worker_id, **tick.as_dict())
                # A full batch means there is probably more behind it; come straight back
                # rather than sleeping through a backlog of expired resources.
                drained = tick.handled >= self.batch_size
            except Exception:
                log.exception("worker tick failed; continuing", worker_id=self.worker_id)
            if drained:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
        log.info("provisioning worker stopped", worker_id=self.worker_id)

    async def tick(self) -> WorkerTick:
        """One pass over both queues.

        Teardown first. When both queues have work, the thing costing money is the
        resource that should already be gone, and a worker that always drains
        provisioning first keeps a backlog of expired resources alive for as long as
        agents keep asking for new ones.
        """
        tick = WorkerTick()
        for lease_id in await self._candidates(TEARDOWN_STATES):
            await self._handle(lease_id, TEARDOWN_STATES, self._deprovision, tick)
        for lease_id in await self._candidates(PROVISION_STATES):
            await self._handle(lease_id, PROVISION_STATES, self._provision, tick)
        return tick

    # -- claiming ----------------------------------------------------------------------

    async def _candidates(self, states: Sequence[LeaseState]) -> list[str]:
        """Ids that look claimable right now. Advisory only: the claim is the real gate."""
        now = utcnow()
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(Lease.id)
                .where(
                    Lease.state.in_([s.value for s in states]),
                    or_(Lease.next_attempt_at.is_(None), Lease.next_attempt_at <= now),
                    or_(
                        Lease.claimed_by.is_(None),
                        Lease.claimed_at.is_(None),
                        Lease.claimed_at < now - CLAIM_TTL,
                    ),
                )
                .order_by(Lease.created_at.asc())
                .limit(self.batch_size)
            )
            return list(result.scalars().all())

    async def _claim(
        self, session: AsyncSession, lease_id: str, states: Sequence[LeaseState]
    ) -> Lease | None:
        """Take exclusive responsibility for a lease, or return ``None``.

        One statement decides it. The ``WHERE`` re-checks the state and the claim, so two
        workers that both selected this id in the same instant cannot both proceed: the
        database applies the updates in some order and the second one matches zero rows.

        The claim is committed immediately, and that is the point. If this process dies on
        the next line, the row records that somebody took a run at it and the attempt
        counter has already moved, so a retry budget cannot be reset by crashing.
        """
        now = utcnow()
        # cast because SQLAlchemy types ``execute`` as returning ``Result``, which has no
        # ``rowcount``; a DML statement really does return a ``CursorResult``. The row
        # count is the entire mechanism here, so it is worth the one narrowing.
        statement = (
            update(Lease)
            .where(
                Lease.id == lease_id,
                Lease.state.in_([s.value for s in states]),
                or_(
                    Lease.claimed_by.is_(None),
                    Lease.claimed_at.is_(None),
                    Lease.claimed_at < now - CLAIM_TTL,
                ),
                or_(Lease.next_attempt_at.is_(None), Lease.next_attempt_at <= now),
            )
            .values(
                claimed_by=self.worker_id,
                claimed_at=now,
                attempts=Lease.attempts + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        claimed = cast("CursorResult[Any]", await session.execute(statement))
        if claimed.rowcount != 1:
            return None
        await session.commit()

        fetched = await session.execute(
            select(Lease).where(Lease.id == lease_id).execution_options(populate_existing=True)
        )
        lease = fetched.scalars().first()
        if lease is None:  # pragma: no cover - the update just matched this row
            return None
        record_event(
            session,
            lease,
            actor=self.worker_id,
            action=ACTION_CLAIMED,
            from_state=lease.state,
            to_state=lease.state,
            detail={"attempt": lease.attempts, "worker_id": self.worker_id},
        )
        return lease

    async def _handle(
        self,
        lease_id: str,
        states: Sequence[LeaseState],
        handler: Handler,
        tick: WorkerTick,
    ) -> None:
        """Claim one lease and run ``handler`` against it in its own transaction.

        A session per lease, deliberately. One long transaction covering a batch would
        mean a provider failure on the eighth lease rolls back the seven before it, which
        would erase the record of resources that really do exist.
        """
        try:
            async with self.sessionmaker() as session:
                try:
                    lease = await self._claim(session, lease_id, states)
                    if lease is None:
                        tick.skipped += 1
                        await session.rollback()
                        return
                    bind_request(
                        lease_id=lease.id,
                        worker_id=self.worker_id,
                        golden_path=lease.golden_path_id,
                        external_name=lease.external_name,
                    )
                    try:
                        await handler(session, lease, tick)
                        await session.commit()
                    finally:
                        clear_request_context()
                except BaseException:
                    await session.rollback()
                    raise
        except Exception:
            # One poisoned lease must not stop the rest of the batch. The claim it holds
            # goes stale on its own and another pass picks it up.
            log.exception("worker failed handling a lease", lease_id=lease_id)
            tick.skipped += 1

    # -- provisioning ------------------------------------------------------------------

    async def _provision(self, session: AsyncSession, lease: Lease, tick: WorkerTick) -> None:
        provider = self._provider_for(session, lease, tick)
        if provider is None:
            return
        if not lease.external_name:
            # Unreachable through the service, which mints and commits the name before
            # the row is visible to a worker. If it ever happens, a resource created for
            # this lease could never be matched back to it, so refusing to call a
            # provider at all is the only safe move.
            self._fail(
                session,
                lease,
                tick,
                "lease has no external_name, so a resource created for it could never be "
                "matched back to it; refusing to provision",
            )
            return

        path = self.catalog.find(lease.golden_path_id)
        inputs = dict(lease.inputs or {})

        # preflight runs while the lease is still PENDING, per the provider contract: a
        # request doomed by a bad input should fail before anything is in flight.
        if lease.lease_state is LeaseState.PENDING:
            try:
                await provider.preflight(external_name=lease.external_name, inputs=inputs)
            except ProviderError as exc:
                await self._provision_failed(session, lease, tick, exc, provider=None)
                return
            transition(
                session,
                lease,
                LeaseState.PROVISIONING,
                actor=self.worker_id,
                action=ACTION_PROVISIONING,
                detail={"attempt": lease.attempts, "external_name": lease.external_name},
            )

        try:
            result = await provider.create(
                external_name=lease.external_name,
                inputs=inputs,
                # Passed so synthetic providers can mint exactly what the path promises.
                # Real providers ignore it; whatever comes back is still filtered against
                # `declared` below, so this widens nothing.
                declared_outputs=[output.name for output in path.outputs] if path else [],
            )
        except ProviderError as exc:
            await self._provision_failed(session, lease, tick, exc, provider=provider)
            return
        except Exception as exc:
            # A bug inside a provider is still a create that may have half-happened.
            log.exception("provider create raised a non-provider error", lease_id=lease.id)
            await self._provision_failed(
                session,
                lease,
                tick,
                ProviderError(
                    lease.provider,
                    f"unexpected {type(exc).__name__} from create",
                    retryable=False,
                    operation="create",
                ),
                provider=provider,
            )
            return

        self._activate(session, lease, path, result)
        tick.provisioned.append(lease.id)

    def _activate(
        self,
        session: AsyncSession,
        lease: Lease,
        path: GoldenPath | None,
        result: ProvisionResult,
    ) -> None:
        """Record a successful create and start the lease clock."""
        now = utcnow()
        lease.provider_resource_id = result.provider_resource_id
        lease.provider_ref = dict(result.provider_ref)
        if result.estimated_hourly_usd > 0:
            lease.estimated_hourly_usd = result.estimated_hourly_usd
        lease.activated_at = now
        # The clock starts here, not at request time. A lease that waited three hours for
        # an approver has not been holding a resource for three hours.
        lease.expires_at = now + timedelta(seconds=lease.ttl_seconds)
        lease.warned_at = None
        lease.failure_reason = None
        lease.claimed_by = None
        lease.claimed_at = None
        lease.next_attempt_at = None
        lease.attempts = 0

        reference: str | None = None
        if result.outputs:
            # The id is materialised here rather than left to the column default,
            # because the reference is derived from it and a column default has not run
            # yet at this point -- ``Binding(...).id`` would be ``None`` and every
            # binding in the deployment would be handed the same reference.
            binding_id = new_id()
            binding = Binding(
                id=binding_id,
                lease_id=lease.id,
                reference=make_reference(binding_id),
                ciphertext=self.secret_box.seal(result.outputs),
                output_names=result.output_names,
                created_at=now,
            )
            session.add(binding)
            reference = binding.reference

        declared = {output.name for output in path.outputs} if path is not None else set()
        public = (
            {output.name for output in path.outputs if not output.secret}
            if path is not None
            else set()
        )
        undeclared = sorted(set(result.outputs) - declared)
        publishable = {name: value for name, value in result.outputs.items() if name in public}

        transition(
            session,
            lease,
            LeaseState.ACTIVE,
            actor=self.worker_id,
            action=ACTION_ACTIVATED,
            detail={
                "external_name": lease.external_name,
                "provider_resource_id": result.provider_resource_id,
                "adopted": result.adopted,
                "expires_at": lease.expires_at.isoformat(),
                "ttl_seconds": lease.ttl_seconds,
                "reference": reference,
                "output_names": result.output_names,
                # Plaintext, and only the names the golden path declares non-secret. This
                # is what every read path renders from, which is how get_lease avoids
                # being a decryption path at all. See engine/service.py.
                "outputs": publishable,
                "estimated_hourly_usd": lease.estimated_hourly_usd,
            },
        )
        if undeclared:
            # An output the catalog never mentioned. It is sealed and not published --
            # anything unclassified is treated as secret -- but it is said out loud,
            # because it means the provider and the golden path have drifted and somebody
            # is about to wonder where their credential went.
            log.warning(
                "provider returned outputs the golden path does not declare; they are "
                "sealed and will not be published",
                lease_id=lease.id,
                golden_path=lease.golden_path_id,
                output_names=undeclared,
            )
        log.info(
            "lease active",
            lease_id=lease.id,
            adopted=result.adopted,
            expires_at=lease.expires_at.isoformat(),
            output_names=result.output_names,
        )

    async def _provision_failed(
        self,
        session: AsyncSession,
        lease: Lease,
        tick: WorkerTick,
        error: ProviderError,
        *,
        provider: Provider | None,
    ) -> None:
        """Schedule another attempt, or give up and clean up after ourselves.

        ``provider`` being ``None`` means the failure happened before anything could have
        been created -- a preflight refusal -- so there is nothing to roll back. When it
        is set, the create call may have left a real resource behind even though it
        raised. That is exactly the half-completed provision
        :meth:`bailment.providers.memory.MemoryProvider.fail_next` can simulate, and the
        give-up path has to go and look rather than assume.
        """
        attempts = lease.attempts
        budget = self.settings.max_provision_attempts
        if error.retryable and attempts < budget:
            delay = backoff_seconds(attempts)
            lease.next_attempt_at = utcnow() + timedelta(seconds=delay)
            lease.claimed_by = None
            lease.claimed_at = None
            lease.failure_reason = error.message
            record_event(
                session,
                lease,
                actor=self.worker_id,
                action=ACTION_PROVISION_FAILED,
                from_state=lease.state,
                to_state=lease.state,
                detail={
                    "error": error.message,
                    "retryable": True,
                    "attempt": attempts,
                    "of": budget,
                    "next_attempt_in_seconds": round(delay, 1),
                },
            )
            tick.retried.append(lease.id)
            log.warning(
                "provision attempt failed; will retry",
                lease_id=lease.id,
                attempt=attempts,
                of=budget,
                error=error.message,
                retry_in_seconds=round(delay, 1),
            )
            return

        reason = (
            f"{error.message} (after {attempts} attempt(s))" if error.retryable else error.message
        )
        if provider is None:
            self._fail(session, lease, tick, reason)
            return
        await self._roll_back(session, lease, tick, provider, reason)

    async def _roll_back(
        self,
        session: AsyncSession,
        lease: Lease,
        tick: WorkerTick,
        provider: Provider,
        reason: str,
    ) -> None:
        """Undo a create that failed, then land the lease somewhere honest.

        Three outcomes and they are all different assertions:

        * the provider says the resource is not there -> ``FAILED``. Nothing exists,
          nobody is billed, the request simply did not work.
        * we destroyed it -> ``FAILED``, with the rollback recorded separately so an
          audit reader can see that something did briefly exist.
        * we could not tell, or could not destroy it -> ``ORPHANED``. Something may be
          alive. Being wrong in this direction costs a line on a dashboard; being wrong
          in the other direction costs a resource nobody will ever look for again.
        """
        external_name = lease.external_name or ""
        provider_ref = dict(lease.provider_ref or {})

        status = ResourceStatus.UNKNOWN
        probe_error: str | None = None
        try:
            status = await provider.exists(external_name=external_name, provider_ref=provider_ref)
        except ProviderError as exc:
            probe_error = exc.message
        except Exception as exc:
            probe_error = f"unexpected {type(exc).__name__} from exists"
            log.exception("provider exists raised during rollback", lease_id=lease.id)

        if status is ResourceStatus.GONE:
            record_event(
                session,
                lease,
                actor=self.worker_id,
                action=ACTION_ROLLED_BACK,
                from_state=lease.state,
                to_state=lease.state,
                detail={"note": "provider reports nothing was created", "error": reason},
            )
            self._fail(session, lease, tick, reason)
            return

        try:
            await provider.destroy(external_name=external_name, provider_ref=provider_ref)
        except ProviderError as exc:
            self._orphan(
                session,
                lease,
                tick,
                f"{reason}; rolling back also failed ({exc.message}), so a resource named "
                f"{external_name!r} may exist at {lease.provider}",
            )
            return
        except Exception as exc:
            log.exception("provider destroy raised during rollback", lease_id=lease.id)
            self._orphan(
                session,
                lease,
                tick,
                f"{reason}; rolling back raised {type(exc).__name__}, so a resource named "
                f"{external_name!r} may exist at {lease.provider}",
            )
            return

        record_event(
            session,
            lease,
            actor=self.worker_id,
            action=ACTION_ROLLED_BACK,
            from_state=lease.state,
            to_state=lease.state,
            detail={
                "note": "partially created resource destroyed",
                "observed_status": status.value,
                "probe_error": probe_error,
                "error": reason,
            },
        )
        self._fail(session, lease, tick, reason)

    def _fail(self, session: AsyncSession, lease: Lease, tick: WorkerTick, reason: str) -> None:
        lease.failure_reason = reason
        lease.next_attempt_at = None
        lease.claimed_by = None
        lease.claimed_at = None
        transition(
            session,
            lease,
            LeaseState.FAILED,
            actor=self.worker_id,
            action=ACTION_PROVISION_FAILED,
            detail={"error": reason, "retryable": False},
        )
        tick.failed.append(lease.id)
        log.error("lease failed", lease_id=lease.id, error=reason)

    # -- teardown ----------------------------------------------------------------------

    async def _deprovision(self, session: AsyncSession, lease: Lease, tick: WorkerTick) -> None:
        provider = self._provider_for(session, lease, tick)
        if provider is None:
            return
        if not lease.external_name:
            self._orphan(
                session,
                lease,
                tick,
                "lease has no external_name; if a resource was ever created for it, "
                "nothing here can identify it",
            )
            return

        if lease.lease_state is not LeaseState.DEPROVISIONING:
            transition(
                session,
                lease,
                LeaseState.DEPROVISIONING,
                actor=self.worker_id,
                action=ACTION_TEARDOWN,
                detail={"attempt": lease.attempts, "external_name": lease.external_name},
            )

        try:
            await provider.destroy(
                external_name=lease.external_name,
                provider_ref=dict(lease.provider_ref or {}),
            )
        except ProviderError as exc:
            self._teardown_failed(session, lease, tick, exc.message, retryable=exc.retryable)
            return
        except Exception as exc:
            log.exception("provider destroy raised a non-provider error", lease_id=lease.id)
            self._teardown_failed(
                session,
                lease,
                tick,
                f"unexpected {type(exc).__name__} from destroy",
                retryable=False,
            )
            return

        self._release(session, lease, tick, note="provider confirmed the resource is gone")

    def _release(self, session: AsyncSession, lease: Lease, tick: WorkerTick, *, note: str) -> None:
        now = utcnow()
        lease.released_at = now
        lease.next_attempt_at = None
        lease.claimed_by = None
        lease.claimed_at = None
        lease.failure_reason = None
        for binding in lease.bindings:
            if binding.revoked_at is None:
                # The credential died with the resource. The ciphertext stays so key
                # rotation tooling can still walk every row; resolve_binding refuses a
                # revoked binding before it would ever open one.
                binding.revoked_at = now
        transition(
            session,
            lease,
            LeaseState.RELEASED,
            actor=self.worker_id,
            action=ACTION_RELEASED,
            detail={"note": note, "external_name": lease.external_name},
        )
        tick.released.append(lease.id)
        log.info("lease released", lease_id=lease.id, external_name=lease.external_name)

    def _teardown_failed(
        self,
        session: AsyncSession,
        lease: Lease,
        tick: WorkerTick,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        """A destroy that did not destroy. This path never ends at RELEASED."""
        attempts = lease.attempts
        budget = self.settings.max_provision_attempts
        if retryable and attempts < budget:
            delay = backoff_seconds(attempts)
            lease.next_attempt_at = utcnow() + timedelta(seconds=delay)
            lease.claimed_by = None
            lease.claimed_at = None
            lease.failure_reason = message
            record_event(
                session,
                lease,
                actor=self.worker_id,
                action=ACTION_TEARDOWN,
                from_state=lease.state,
                to_state=lease.state,
                detail={
                    "error": message,
                    "retryable": True,
                    "attempt": attempts,
                    "of": budget,
                    "next_attempt_in_seconds": round(delay, 1),
                },
            )
            tick.retried.append(lease.id)
            log.warning(
                "teardown attempt failed; will retry",
                lease_id=lease.id,
                attempt=attempts,
                of=budget,
                error=message,
            )
            return
        self._orphan(
            session,
            lease,
            tick,
            f"{message} (after {attempts} attempt(s)); the resource may still exist",
        )

    def _orphan(self, session: AsyncSession, lease: Lease, tick: WorkerTick, reason: str) -> None:
        lease.failure_reason = reason
        lease.next_attempt_at = None
        lease.claimed_by = None
        lease.claimed_at = None
        self._to_orphaned(session, lease, reason)
        tick.orphaned.append(lease.id)
        log.error(
            "lease orphaned",
            lease_id=lease.id,
            external_name=lease.external_name,
            provider=lease.provider,
            reason=reason,
        )

    def _to_orphaned(self, session: AsyncSession, lease: Lease, reason: str) -> None:
        """Reach ORPHANED by the route the state machine actually permits.

        From ``DEPROVISIONING`` the edge exists directly. From ``PROVISIONING`` it does
        not, and routing through ``UNKNOWN`` is the honest sequence: we could not
        determine what the provider did, and *therefore* we now believe there may be an
        unowned resource. Two audit rows, two different assertions, neither invented.
        """
        source = lease.lease_state
        if source is not LeaseState.ORPHANED and LeaseState.ORPHANED not in TRANSITIONS[source]:
            transition(
                session,
                lease,
                LeaseState.UNKNOWN,
                actor=self.worker_id,
                action=ACTION_UNKNOWN_STATE,
                detail={"reason": reason},
            )
        transition(
            session,
            lease,
            LeaseState.ORPHANED,
            actor=self.worker_id,
            action=ACTION_ORPHANED,
            detail={
                "reason": reason,
                "external_name": lease.external_name,
                "provider": lease.provider,
                "note": (
                    "a resource may exist at the provider with no live lease behind it; "
                    "the reconciler will keep reporting it until it is destroyed"
                ),
            },
        )

    # -- shared ------------------------------------------------------------------------

    def _provider_for(
        self, session: AsyncSession, lease: Lease, tick: WorkerTick
    ) -> Provider | None:
        """Resolve the provider, parking the lease if it cannot be used.

        A provider that is registered but unconfigured is not a lease failure -- somebody
        forgot an environment variable and will set it -- so the lease is rescheduled
        rather than killed. A provider that is not registered at all is a catalog error
        that waiting will not fix, but the lease is still parked rather than failed,
        because failing a teardown would abandon a resource on the strength of a typo.
        """
        try:
            provider = self.registry.get(lease.provider)
        except UnknownProvider as exc:
            self._park(session, lease, tick, str(exc))
            return None
        if not provider.is_available():
            self._park(
                session,
                lease,
                tick,
                f"provider {lease.provider!r} is registered but not configured; the lease "
                f"will be retried once its credentials are present",
            )
            return None
        return provider

    def _park(self, session: AsyncSession, lease: Lease, tick: WorkerTick, reason: str) -> None:
        """Release the claim and try later, without consuming the retry budget.

        The attempt this pass consumed is handed back. The failure is in the deployment,
        not in the request, and spending a lease's budget while an operator fetches an
        API key would destroy work for a reason its requester can neither see nor fix.
        """
        lease.attempts = max(0, lease.attempts - 1)
        lease.claimed_by = None
        lease.claimed_at = None
        lease.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds(1, base=30.0))
        lease.failure_reason = reason
        record_event(
            session,
            lease,
            actor=self.worker_id,
            action=ACTION_PROVISION_FAILED,
            from_state=lease.state,
            to_state=lease.state,
            detail={"error": reason, "parked": True},
        )
        tick.skipped += 1
        log.warning("lease parked", lease_id=lease.id, provider=lease.provider, reason=reason)
