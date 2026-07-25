"""Drift detection in both directions. This is the part of bailment that is hard.

Credential brokering is a solved problem and several products do it well. What none of
them do is answer the question a platform team actually gets asked, which is *"is there
anything running right now that nobody is accounting for?"* A broker knows what it
intended. Only a reconciler knows what is true.

There are two directions and they fail in completely different ways.

**Direction 1 -- orphans.** Ask the provider what exists, compare against the lease table.
A resource carrying bailment's marker that no live lease accounts for is an orphan: it is
running, it is billing, and nothing in the system is going to end it. These come from
half-completed provisions, from teardowns that failed after the retry budget ran out, and
from a worker that was killed at exactly the wrong moment. Every provisioning system
produces them. Most cannot see them.

**Direction 2 -- vanished resources.** Ask about each lease we believe is live. A resource
that is GONE was deleted underneath us -- usually by a person in a console, occasionally
by the provider itself -- and the lease is now a promise about something that does not
exist. Left alone it keeps a name reserved, keeps a binding resolvable, and keeps a slot
in whatever quota policy counts.

Four rules govern the whole module.

**Report, do not delete.** Destroying orphans is off by default and requires *two*
switches: the global ``reconcile_auto_destroy_orphans`` and this provider's name in
``destroy_orphans_for``. A tool that deletes cloud resources on its first run because
somebody left a default on is a tool nobody installs twice, and the blast radius of the
mistake is somebody's hand-made database. Two switches means nobody arrives here by
accident, and every armed run says so at warning level before it does anything.

**Nothing younger than the grace period is ever flagged.** Ten minutes by default. Without
it the reconciler races in-flight provisioning: a resource created ninety seconds ago by a
worker that has not yet committed its result looks exactly like an orphan, and the
reconciler reports healthy work as drift -- or, with destruction armed, deletes a database
somebody is halfway through being given. A resource whose age cannot be determined at all
is reported but never destroyed.

**UNKNOWN changes nothing.** Not the state, not a timestamp, not an audit row. A provider
that times out has told us nothing, and the one thing this module must never do is turn
silence into ``RELEASED``. It is counted, so that a provider which answers UNKNOWN for a
week is visible as a broken integration rather than as a clean account.

**No transaction is held open across a network call.** Each provider is reconciled in
three phases: read a snapshot of the lease rows, do every provider call with no
transaction open, then write. The writes re-read each row and re-check that its state is
still the one that was observed, because a worker may have moved it while the provider was
being asked. Reconciling on stale reads is how a reconciler starts causing the drift it
exists to find.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.config import Settings, get_settings
from bailment.engine.service import (
    ACTION_RECONCILE_DRIFT,
    ACTION_RELEASED,
    ACTION_UNKNOWN_STATE,
    aware,
    transition,
)
from bailment.engine.worker import CLAIM_TTL
from bailment.logging import get_logger
from bailment.models import Lease, ReconcileRun, utcnow
from bailment.providers.base import (
    ManagedResource,
    Provider,
    ProviderError,
    ResourceStatus,
    is_managed_name,
)
from bailment.providers.registry import ProviderRegistry
from bailment.states import LIVE_STATES, TERMINAL_STATES, TRANSITIONS, LeaseState

__all__ = [
    "DEFAULT_GRACE",
    "DriftRecord",
    "OrphanRecord",
    "ProviderReconcileResult",
    "ReconcileSummary",
    "Reconciler",
]

log = get_logger("bailment.engine.reconciler")

#: How new a resource has to be before the reconciler leaves it alone. See the module
#: docstring: without this the reconciler races its own workers.
DEFAULT_GRACE: Final = timedelta(minutes=10)

#: The actor written into every audit row this module produces. One string, so "what did
#: the reconciler do overnight" is a single filter rather than a guess about spelling.
RECONCILER: Final = "bailment:reconciler"


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrphanRecord:
    """A resource that exists at the provider with no live lease behind it."""

    provider: str
    external_name: str
    provider_resource_id: str
    reason: str
    created_at: datetime | None
    age_seconds: int | None
    lease_id: str | None
    lease_state: str | None
    destroyed: bool = False
    destroy_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_name": self.external_name,
            "provider_resource_id": self.provider_resource_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "age_seconds": self.age_seconds,
            "lease_id": self.lease_id,
            "lease_state": self.lease_state,
            "destroyed": self.destroyed,
            "destroy_error": self.destroy_error,
        }


@dataclass(frozen=True, slots=True)
class DriftRecord:
    """A lease we believed was live whose resource has vanished."""

    provider: str
    lease_id: str
    external_name: str
    from_state: str
    to_state: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "lease_id": self.lease_id,
            "external_name": self.external_name,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "note": self.note,
        }


@dataclass(slots=True)
class ProviderReconcileResult:
    """One provider's pass. Mirrors the :class:`~bailment.models.ReconcileRun` row."""

    provider: str
    started_at: datetime
    finished_at: datetime | None = None
    run_id: str | None = None
    checked: bool = True
    skipped_reason: str | None = None
    destroy_armed: bool = False

    resources_seen: int = 0
    leases_checked: int = 0
    orphans: list[OrphanRecord] = field(default_factory=list)
    drift: list[DriftRecord] = field(default_factory=list)

    status_unknown: list[str] = field(default_factory=list)
    """Leases whose resource status could not be determined. Nothing was changed."""

    within_grace: int = 0
    in_flight_skipped: int = 0
    raced: int = 0
    """Rows a worker moved between the snapshot and the write. Left alone, re-checked next run."""

    known_orphan_leases: int = 0
    """Leases already sitting in ORPHANED. Not re-flagged, but the number nobody wants to grow."""

    errors: list[str] = field(default_factory=list)

    @property
    def orphans_destroyed(self) -> int:
        return sum(1 for orphan in self.orphans if orphan.destroyed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "run_id": self.run_id,
            "checked": self.checked,
            "skipped_reason": self.skipped_reason,
            "destroy_armed": self.destroy_armed,
            "resources_seen": self.resources_seen,
            "leases_checked": self.leases_checked,
            "orphans_found": len(self.orphans),
            "orphans_destroyed": self.orphans_destroyed,
            "drift_found": len(self.drift),
            "status_unknown": list(self.status_unknown),
            "within_grace": self.within_grace,
            "in_flight_skipped": self.in_flight_skipped,
            "raced": self.raced,
            "known_orphan_leases": self.known_orphan_leases,
            "errors": list(self.errors),
            "orphans": [o.as_dict() for o in self.orphans],
            "drift": [d.as_dict() for d in self.drift],
        }


@dataclass(slots=True)
class ReconcileSummary:
    """The whole run. This is what the dashboard's headline metric renders from."""

    started_at: datetime
    finished_at: datetime | None = None
    providers: list[ProviderReconcileResult] = field(default_factory=list)

    @property
    def orphans_found(self) -> int:
        return sum(len(p.orphans) for p in self.providers)

    @property
    def orphans_destroyed(self) -> int:
        return sum(p.orphans_destroyed for p in self.providers)

    @property
    def drift_found(self) -> int:
        return sum(len(p.drift) for p in self.providers)

    @property
    def resources_seen(self) -> int:
        return sum(p.resources_seen for p in self.providers)

    @property
    def leases_checked(self) -> int:
        return sum(p.leases_checked for p in self.providers)

    @property
    def status_unknown(self) -> int:
        return sum(len(p.status_unknown) for p in self.providers)

    @property
    def errors(self) -> int:
        return sum(len(p.errors) for p in self.providers)

    @property
    def providers_checked(self) -> tuple[str, ...]:
        return tuple(p.provider for p in self.providers if p.checked)

    @property
    def providers_skipped(self) -> tuple[str, ...]:
        return tuple(p.provider for p in self.providers if not p.checked)

    @property
    def clean(self) -> bool:
        """Whether this run found nothing wrong *and* was able to look everywhere.

        A run that could not reach a provider is not clean, however empty its counters
        are. Conflating "nothing found" with "nothing checked" is the single most likely
        way for a drift dashboard to be reassuring and wrong.
        """
        return (
            self.orphans_found == 0
            and self.drift_found == 0
            and self.errors == 0
            and self.status_unknown == 0
            and not self.providers_skipped
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "orphans_found": self.orphans_found,
            "orphans_destroyed": self.orphans_destroyed,
            "drift_found": self.drift_found,
            "resources_seen": self.resources_seen,
            "leases_checked": self.leases_checked,
            "status_unknown": self.status_unknown,
            "errors": self.errors,
            "providers_checked": list(self.providers_checked),
            "providers_skipped": list(self.providers_skipped),
            "clean": self.clean,
            "providers": [p.as_dict() for p in self.providers],
        }


# --------------------------------------------------------------------------------------
# Snapshot -- what phase 1 reads and phase 2 works from
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """A lease row as it looked before any provider was asked about it.

    A plain value rather than an ORM object because phase 2 makes network calls with no
    session open, and a detached instance whose lazy relationships are gone is a trap
    waiting for whoever adds the next attribute access.
    """

    id: str
    state: LeaseState
    external_name: str
    provider_ref: dict[str, Any]
    created_at: datetime
    activated_at: datetime | None
    claimed_at: datetime | None
    claimed_by: str | None

    @property
    def age_reference(self) -> datetime:
        """When this lease's resource plausibly started existing."""
        return self.activated_at or self.created_at

    def claim_is_stale(self, now: datetime) -> bool:
        if self.claimed_by is None or self.claimed_at is None:
            return True
        return now - self.claimed_at > CLAIM_TTL


# --------------------------------------------------------------------------------------
# The reconciler
# --------------------------------------------------------------------------------------


class Reconciler:
    """Compares provider reality against the lease table, in both directions."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        grace: timedelta = DEFAULT_GRACE,
        destroy_orphans_for: Iterable[str] = (),
    ) -> None:
        self.sessionmaker = sessionmaker
        self.registry = registry
        self.settings = settings or get_settings()
        self.grace = grace
        self.destroy_orphans_for = frozenset(destroy_orphans_for)
        """Providers whose orphans may be destroyed -- *and only* if the global switch is
        also on. Per-provider because trusting bailment's tagging on a sandbox account is
        a different decision from trusting it on the account that holds production."""

    # -- arming ------------------------------------------------------------------------

    def may_destroy(self, provider_name: str) -> bool:
        """Both switches, or nothing happens."""
        return (
            self.settings.reconcile_auto_destroy_orphans
            and provider_name in self.destroy_orphans_for
        )

    @property
    def armed_providers(self) -> tuple[str, ...]:
        return tuple(sorted(p for p in self.destroy_orphans_for if self.may_destroy(p)))

    # -- loop --------------------------------------------------------------------------

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        interval = float(self.settings.reconcile_interval_seconds)
        log.info(
            "reconciler started",
            interval_seconds=interval,
            grace_seconds=int(self.grace.total_seconds()),
            destroy_armed_for=list(self.armed_providers),
        )
        while not stop.is_set():
            try:
                summary = await self.run()
                log.info("reconcile complete", **_headline(summary))
            except Exception:
                log.exception("reconcile run failed; continuing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
        log.info("reconciler stopped")

    async def run(self) -> ReconcileSummary:
        """One full pass over every provider. Returns the structured summary."""
        summary = ReconcileSummary(started_at=utcnow())
        armed = self.armed_providers
        if armed:
            # Loud, every run, on purpose. Somebody should be able to grep for the day
            # this was turned on.
            log.warning(
                "reconciler orphan destruction is ARMED; orphaned resources older than "
                "the grace period will be DELETED at these providers",
                providers=list(armed),
                grace_seconds=int(self.grace.total_seconds()),
            )

        for name in self.registry.names():
            provider = self.registry.get(name)
            skip = _skip_reason(provider)
            if skip is not None:
                summary.providers.append(
                    ProviderReconcileResult(
                        provider=name,
                        started_at=utcnow(),
                        finished_at=utcnow(),
                        checked=False,
                        skipped_reason=skip,
                    )
                )
                continue
            try:
                summary.providers.append(await self._reconcile_provider(provider))
            except Exception as exc:
                log.exception("provider reconcile failed", provider=name)
                summary.providers.append(
                    ProviderReconcileResult(
                        provider=name,
                        started_at=utcnow(),
                        finished_at=utcnow(),
                        checked=False,
                        skipped_reason=f"reconcile raised {type(exc).__name__}",
                        errors=[f"{type(exc).__name__}: {exc}"],
                    )
                )
        summary.finished_at = utcnow()
        return summary

    # -- one provider ------------------------------------------------------------------

    async def _reconcile_provider(self, provider: Provider) -> ProviderReconcileResult:
        name = provider.name
        result = ProviderReconcileResult(
            provider=name, started_at=utcnow(), destroy_armed=self.may_destroy(name)
        )
        now = result.started_at

        # Phase 1: read. No provider has been called yet, and the transaction closes
        # before one is.
        snapshots = await self._snapshot(name)
        by_name = {snap.external_name: snap for snap in snapshots}
        result.known_orphan_leases = sum(
            1 for snap in snapshots if snap.state is LeaseState.ORPHANED
        )

        # Phase 2: ask the provider. Nothing is open, nothing is locked.
        orphans = await self._find_orphans(provider, by_name, result, now)
        drift, unknown = await self._find_drift(provider, snapshots, result, now)
        result.orphans = orphans
        result.status_unknown = unknown

        # Phase 3: write. Every row is re-read and re-checked first.
        result.drift = await self._apply_drift(name, drift, result)
        result.finished_at = utcnow()
        await self._record_run(result)

        if result.orphans or result.drift or result.errors:
            log.warning(
                "reconcile found drift",
                provider=name,
                orphans_found=len(result.orphans),
                orphans_destroyed=result.orphans_destroyed,
                drift_found=len(result.drift),
                status_unknown=len(result.status_unknown),
                errors=len(result.errors),
            )
        return result

    async def _snapshot(self, provider_name: str) -> list[_Snapshot]:
        """Every lease for this provider that ever got as far as being named.

        Deliberately unbounded. A reconciler that pages and stops halfway reports the
        resources it did not reach as absent from the lease table, which is precisely the
        false orphan the grace period exists to avoid -- except this one would be caused
        by us.
        """
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(
                    Lease.id,
                    Lease.state,
                    Lease.external_name,
                    Lease.provider_ref,
                    Lease.created_at,
                    Lease.activated_at,
                    Lease.claimed_at,
                    Lease.claimed_by,
                ).where(
                    Lease.provider == provider_name,
                    Lease.external_name.is_not(None),
                )
            )
            snapshots: list[_Snapshot] = []
            for row in result.all():
                if not row.external_name:
                    continue
                snapshots.append(
                    _Snapshot(
                        id=row.id,
                        state=LeaseState(row.state),
                        external_name=row.external_name,
                        provider_ref=dict(row.provider_ref or {}),
                        created_at=aware(row.created_at),
                        activated_at=aware(row.activated_at) if row.activated_at else None,
                        claimed_at=aware(row.claimed_at) if row.claimed_at else None,
                        claimed_by=row.claimed_by,
                    )
                )
            return snapshots

    # -- direction 1: orphans ----------------------------------------------------------

    async def _find_orphans(
        self,
        provider: Provider,
        by_name: dict[str, _Snapshot],
        result: ProviderReconcileResult,
        now: datetime,
    ) -> list[OrphanRecord]:
        """Enumerate what exists and subtract what we can account for."""
        try:
            resources = await provider.list_managed()
        except ProviderError as exc:
            # An empty list would read as "the account is clean". It is not; we simply
            # could not see it. Direction 2 still runs, because a lease-by-lease check
            # does not depend on enumeration working.
            result.errors.append(f"list_managed: {exc.message}")
            log.warning("could not enumerate resources", provider=provider.name, error=exc.message)
            return []
        except Exception as exc:
            result.errors.append(f"list_managed: unexpected {type(exc).__name__}")
            log.exception("list_managed raised", provider=provider.name)
            return []

        result.resources_seen = len(resources)
        orphans: list[OrphanRecord] = []
        for resource in resources:
            record = self._classify(resource, by_name, result, now, provider.name)
            if record is None:
                continue
            orphans.append(await self._maybe_destroy(provider, record))
        return orphans

    def _classify(
        self,
        resource: ManagedResource,
        by_name: dict[str, _Snapshot],
        result: ProviderReconcileResult,
        now: datetime,
        provider_name: str,
    ) -> OrphanRecord | None:
        """Decide whether one resource is an orphan. ``None`` means it is accounted for."""
        if not is_managed_name(resource.external_name):
            # A provider whose server-side filter widened in a new API version. The
            # blast radius of trusting it is "delete something a human made by hand".
            result.errors.append(
                f"list_managed returned {resource.external_name!r}, which does not carry "
                f"bailment's prefix; ignored"
            )
            log.error(
                "provider returned an unmanaged resource from list_managed",
                provider=provider_name,
                external_name=resource.external_name,
            )
            return None

        snapshot = by_name.get(resource.external_name)
        created = aware(resource.created_at) if resource.created_at else None
        reference = created or (snapshot.created_at if snapshot is not None else None)
        age = now - reference if reference is not None else None

        if age is not None and age < self.grace:
            # In-flight work. Reporting it would be reporting a healthy provision.
            result.within_grace += 1
            return None

        if snapshot is None:
            reason = (
                "no lease in this database names this resource. It was created by a "
                "worker that died before recording it, or by a bailment install that no "
                "longer exists"
            )
        elif snapshot.state in TERMINAL_STATES:
            reason = (
                f"the lease that named it is {snapshot.state.value}, so bailment believes "
                f"this resource does not exist -- but the provider says it does"
            )
        else:
            # A live lease accounts for it, including ORPHANED, which is already counted
            # separately. Nothing to report.
            return None

        return OrphanRecord(
            provider=provider_name,
            external_name=resource.external_name,
            provider_resource_id=resource.provider_resource_id,
            reason=reason,
            created_at=created,
            age_seconds=int(age.total_seconds()) if age is not None else None,
            lease_id=snapshot.id if snapshot is not None else None,
            lease_state=snapshot.state.value if snapshot is not None else None,
        )

    async def _maybe_destroy(self, provider: Provider, orphan: OrphanRecord) -> OrphanRecord:
        """Destroy an orphan, but only if everything says it is safe to.

        Both switches, and a known age past the grace period. An orphan whose age could
        not be established is reported and left alone: the resource may be thirty seconds
        old and mid-provision, and no amount of configuration should let the reconciler
        guess about that.
        """
        if not self.may_destroy(orphan.provider):
            log.warning(
                "ORPHAN FOUND (report only)",
                provider=orphan.provider,
                external_name=orphan.external_name,
                provider_resource_id=orphan.provider_resource_id,
                age_seconds=orphan.age_seconds,
                lease_id=orphan.lease_id,
                lease_state=orphan.lease_state,
                reason=orphan.reason,
            )
            return orphan
        if orphan.age_seconds is None:
            log.warning(
                "ORPHAN FOUND, age unknown; refusing to destroy something that might be "
                "mid-provision",
                provider=orphan.provider,
                external_name=orphan.external_name,
            )
            return orphan

        log.warning(
            "DESTROYING ORPHAN",
            provider=orphan.provider,
            external_name=orphan.external_name,
            provider_resource_id=orphan.provider_resource_id,
            age_seconds=orphan.age_seconds,
            reason=orphan.reason,
        )
        ref: dict[str, Any] = {"name": orphan.external_name}
        if orphan.provider_resource_id:
            ref["resource_id"] = orphan.provider_resource_id
        try:
            await provider.destroy(external_name=orphan.external_name, provider_ref=ref)
        except ProviderError as exc:
            log.error(
                "orphan destroy failed",
                provider=orphan.provider,
                external_name=orphan.external_name,
                error=exc.message,
            )
            return _replace_orphan(orphan, destroyed=False, destroy_error=exc.message)
        except Exception as exc:
            log.exception("orphan destroy raised", provider=orphan.provider)
            return _replace_orphan(
                orphan, destroyed=False, destroy_error=f"unexpected {type(exc).__name__}"
            )
        log.warning(
            "ORPHAN DESTROYED",
            provider=orphan.provider,
            external_name=orphan.external_name,
        )
        return _replace_orphan(orphan, destroyed=True, destroy_error=None)

    # -- direction 2: vanished resources -----------------------------------------------

    async def _find_drift(
        self,
        provider: Provider,
        snapshots: Sequence[_Snapshot],
        result: ProviderReconcileResult,
        now: datetime,
    ) -> tuple[list[tuple[_Snapshot, LeaseState]], list[str]]:
        """Ask about every live lease. Returns intended transitions and unknown ids."""
        intended: list[tuple[_Snapshot, LeaseState]] = []
        unknown: list[str] = []
        for snapshot in snapshots:
            if snapshot.state not in LIVE_STATES:
                continue
            if not snapshot.claim_is_stale(now):
                # A worker is holding this row right now. Asking the provider mid-call
                # and acting on the answer is how a reconciler races a create.
                result.in_flight_skipped += 1
                continue
            if now - snapshot.age_reference < self.grace:
                result.within_grace += 1
                continue

            try:
                status = await provider.exists(
                    external_name=snapshot.external_name, provider_ref=snapshot.provider_ref
                )
            except ProviderError as exc:
                result.errors.append(f"exists({snapshot.external_name}): {exc.message}")
                unknown.append(snapshot.id)
                continue
            except Exception as exc:
                result.errors.append(
                    f"exists({snapshot.external_name}): unexpected {type(exc).__name__}"
                )
                log.exception("provider exists raised", provider=provider.name)
                unknown.append(snapshot.id)
                continue

            result.leases_checked += 1
            if status is ResourceStatus.EXISTS:
                continue
            if status is ResourceStatus.UNKNOWN:
                # The whole point. Nothing is written, nothing is inferred, and the
                # counter makes a provider that never knows anything visible as a broken
                # integration rather than as a clean account.
                unknown.append(snapshot.id)
                continue
            intended.append((snapshot, _vanished_target(snapshot)))
        return intended, unknown

    async def _apply_drift(
        self,
        provider_name: str,
        intended: Sequence[tuple[_Snapshot, LeaseState]],
        result: ProviderReconcileResult,
    ) -> list[DriftRecord]:
        """Write the drift conclusions, re-checking each row first."""
        if not intended:
            return []
        records: list[DriftRecord] = []
        async with self.sessionmaker() as session:
            try:
                for snapshot, target in intended:
                    lease = await session.get(Lease, snapshot.id)
                    if lease is None:  # pragma: no cover - snapshot came from this table
                        continue
                    if lease.lease_state is not snapshot.state:
                        # A worker moved it while we were talking to the provider. Its
                        # answer is about a row that no longer exists in that form.
                        result.raced += 1
                        continue

                    note = (
                        "reconciler observed external deletion: the provider reports this "
                        "resource does not exist, so it was destroyed outside bailment"
                    )
                    from_state = lease.state
                    _route(session, lease, target, note=note, provider_name=provider_name)
                    if target is LeaseState.RELEASED:
                        now = utcnow()
                        lease.released_at = now
                        for binding in lease.bindings:
                            # The credential is dead: the thing it opened is gone.
                            if binding.revoked_at is None:
                                binding.revoked_at = now
                    else:
                        lease.failure_reason = note
                    lease.next_attempt_at = None
                    lease.claimed_by = None
                    lease.claimed_at = None
                    records.append(
                        DriftRecord(
                            provider=provider_name,
                            lease_id=lease.id,
                            external_name=lease.external_name or snapshot.external_name,
                            from_state=from_state,
                            to_state=lease.state,
                            note=note,
                        )
                    )
                    log.warning(
                        "resource vanished underneath a live lease",
                        lease_id=lease.id,
                        provider=provider_name,
                        external_name=lease.external_name,
                        from_state=from_state,
                        to_state=lease.state,
                    )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return records

    # -- history -----------------------------------------------------------------------

    async def _record_run(self, result: ProviderReconcileResult) -> None:
        """Persist the run so drift can be tracked over time.

        Only providers that were actually checked get a row. A run row for a provider
        nobody could reach would read as a clean sweep in every ``orphans_found`` chart
        anybody ever builds off this table, and "we did not look" must never render as
        "there was nothing there".
        """
        run = ReconcileRun(
            provider=result.provider,
            started_at=result.started_at,
            finished_at=result.finished_at,
            resources_seen=result.resources_seen,
            leases_checked=result.leases_checked,
            orphans_found=len(result.orphans),
            orphans_destroyed=result.orphans_destroyed,
            drift_found=len(result.drift),
            errors=len(result.errors),
            detail={
                "destroy_armed": result.destroy_armed,
                "grace_seconds": int(self.grace.total_seconds()),
                "within_grace": result.within_grace,
                "in_flight_skipped": result.in_flight_skipped,
                "raced": result.raced,
                "known_orphan_leases": result.known_orphan_leases,
                "status_unknown": list(result.status_unknown),
                "errors": list(result.errors),
                "orphans": [o.as_dict() for o in result.orphans],
                "drift": [d.as_dict() for d in result.drift],
            },
        )
        async with self.sessionmaker() as session:
            try:
                session.add(run)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        result.run_id = run.id


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _skip_reason(provider: Provider) -> str | None:
    """Why this provider will not be swept, or ``None`` if it will be.

    Both reasons are reported rather than silently filtered, because a dashboard that
    only lists what worked cannot distinguish "no orphans at Cloudflare" from "Cloudflare
    was never asked".
    """
    if not provider.is_available():
        return "provider is not configured, so it cannot be asked what exists"
    if not provider.supports_reconciliation:
        return (
            "provider cannot enumerate its own resources by bailment's marker, so an "
            "orphan sweep would report an empty account rather than an unknown one"
        )
    return None


def _vanished_target(snapshot: _Snapshot) -> LeaseState:
    """Where a lease should land once its resource is confirmed gone.

    ``RELEASED`` means "the resource this lease held is gone", which is only true of a
    lease that ever held one. A lease that vanished while still provisioning never became
    usable, and calling that released would put a lease in the history books as having
    successfully served a resource it never delivered. ``FAILED`` is the honest word.
    """
    return LeaseState.RELEASED if snapshot.activated_at is not None else LeaseState.FAILED


def _route(
    session: AsyncSession, lease: Lease, target: LeaseState, *, note: str, provider_name: str
) -> None:
    """Move a lease to ``target`` via ``UNKNOWN`` when the direct edge does not exist.

    ``ACTIVE -> RELEASED`` is not a legal transition and should not be: releasing is
    something only a confirmed teardown does. What the reconciler actually learned is two
    things in sequence -- we no longer know the state of this resource, and then, we have
    established it is gone -- so it says both, and both land in the audit trail.
    """
    detail = {
        "note": note,
        "external_name": lease.external_name,
        "provider": provider_name,
        "observed_by": "reconciler",
    }
    source = lease.lease_state
    if source is not target and target not in TRANSITIONS[source]:
        transition(
            session,
            lease,
            LeaseState.UNKNOWN,
            actor=RECONCILER,
            action=ACTION_UNKNOWN_STATE,
            detail=detail,
        )
    transition(
        session,
        lease,
        target,
        actor=RECONCILER,
        action=ACTION_RELEASED if target is LeaseState.RELEASED else ACTION_RECONCILE_DRIFT,
        detail=detail,
    )


def _replace_orphan(
    orphan: OrphanRecord, *, destroyed: bool, destroy_error: str | None
) -> OrphanRecord:
    return OrphanRecord(
        provider=orphan.provider,
        external_name=orphan.external_name,
        provider_resource_id=orphan.provider_resource_id,
        reason=orphan.reason,
        created_at=orphan.created_at,
        age_seconds=orphan.age_seconds,
        lease_id=orphan.lease_id,
        lease_state=orphan.lease_state,
        destroyed=destroyed,
        destroy_error=destroy_error,
    )


def _headline(summary: ReconcileSummary) -> dict[str, Any]:
    return {
        "orphans_found": summary.orphans_found,
        "orphans_destroyed": summary.orphans_destroyed,
        "drift_found": summary.drift_found,
        "resources_seen": summary.resources_seen,
        "leases_checked": summary.leases_checked,
        "status_unknown": summary.status_unknown,
        "errors": summary.errors,
        "providers_checked": list(summary.providers_checked),
        "providers_skipped": list(summary.providers_skipped),
        "clean": summary.clean,
    }
