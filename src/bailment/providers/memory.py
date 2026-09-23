"""A provider that behaves like a real one, including badly -- and that every process shares.

This is not a stub. It creates resources, hands back credentials, refuses to
double-provision on retry, distinguishes a missing resource from an unreachable one, and
enumerates only the resources carrying bailment's marker -- the same contract the Neon,
Upstash and Cloudflare providers meet against real APIs.

What it adds is the ability to *choose* the failure. Every interesting property of this
system is a property of its failure paths, and those paths are exactly the ones you
cannot trigger on demand against a real cloud API:

* :meth:`MemoryProvider.fail_next` with ``half_succeed=True`` -- the create call that
  really does create the resource and then fails to report it. The lease ends up FAILED
  while the resource bills. This is the orphan the reconciler exists to find, and without
  a hook like this it can only be produced by killing a worker at the right microsecond.
* :meth:`MemoryProvider.fail_next` on ``destroy`` -- teardown that does not tear down,
  which must drive the lease to ORPHANED and never to RELEASED.
* :meth:`MemoryProvider.avanish` -- a resource deleted out of band, by a human in a
  console. The lease still says ACTIVE. This is drift in the other direction.
* :meth:`MemoryProvider.aplant_orphan` -- a resource no lease has ever heard of.
* :meth:`MemoryProvider.arelent` -- the out-of-band fix that finally lets a stuck
  teardown through, so a demo orphan has an ending.
* :meth:`MemoryProvider.force_status` -- a resource whose status cannot be determined, so
  ``exists`` answers UNKNOWN and the system has to prove it does not treat that as GONE.

**Where the resources live.** Behind a :class:`ResourceStore`, and which one matters more
than anything else in this file.

:class:`InProcessStore` keeps them in a dict. It is what a bare ``MemoryProvider()`` gets,
it is what the test suite uses, and it is only honest when one process does everything.

:class:`SqlResourceStore` keeps them in the ``memory_resources`` table of bailment's own
database, and it is what the provider the registry hands out uses. That is not a
convenience. The API, the worker and ``bailment reconcile`` in an operator's terminal are
separate processes in every deployment shape bailment ships -- ``docker compose`` puts the
API and the worker in separate containers, and the README's own quickstart runs the
reconciler from a second shell. With a per-process dict, each of them asked an empty
provider what existed, and the reconciler -- correctly, from what it was told -- marked
healthy leases RELEASED and revoked their bindings while the resources were alive in
another process. A fake cloud that each process sees a different copy of is not a fake
cloud; it is a way to manufacture drift.

The fault hooks (:meth:`~MemoryProvider.fail_next`, :meth:`~MemoryProvider.force_status`,
:meth:`~MemoryProvider.set_unavailable`) stay process-local on purpose: they are test
instruments, armed by the code that is about to observe the failure.
"""

from __future__ import annotations

import asyncio
import random
import secrets
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from bailment.models import MemoryResourceRecord, utcnow
from bailment.providers.base import (
    ManagedResource,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
    coerce_str,
    is_managed_name,
    require_managed_name,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bailment.secrets import SecretBox

__all__ = [
    "InProcessStore",
    "MemoryProvider",
    "MemoryResource",
    "ResourceStore",
    "SqlResourceStore",
    "default_memory_provider",
]

Operation = str  # one of: preflight, create, destroy, exists, list_managed

#: Behaviours a caller can ask for through the ``simulate`` input.
#:
#: :meth:`MemoryProvider.fail_next` arms a fault out of band, which is what tests want. This
#: is the other half: a fault a *requester* selects, declared in the golden path's input
#: schema, so the failure paths can be walked through from the dashboard or an MCP tool by
#: somebody who is evaluating bailment and has no intention of writing a test.
#:
#: Any golden path on this provider can offer these by adding a ``simulate`` input; the
#: shipped ``sandbox`` path does. A path that omits it gets ``ok`` and never fails.
SIMULATE_OK = "ok"
SIMULATE_SLOW_CREATE = "slow_create"
SIMULATE_CREATE_FAILURE = "create_failure"
SIMULATE_DESTROY_FAILURE = "destroy_failure"

SIMULATIONS: frozenset[str] = frozenset(
    {SIMULATE_OK, SIMULATE_SLOW_CREATE, SIMULATE_CREATE_FAILURE, SIMULATE_DESTROY_FAILURE}
)

#: How long ``slow_create`` takes. Long enough to watch a lease sit in PROVISIONING in the
#: dashboard, short enough that nobody assumes it has hung.
SLOW_CREATE_SECONDS = 12.0


@dataclass(slots=True)
class MemoryResource:
    """A resource that "exists" at the fake provider."""

    external_name: str
    resource_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    outputs: dict[str, str] = field(default_factory=dict)
    """Held so an adopted retry returns the same credential rather than a fresh one.

    Always populated by :class:`InProcessStore`. :class:`SqlResourceStore` keeps them sealed
    and fills this in only through :meth:`ResourceStore.outputs`, so listing what exists
    never needs -- or touches -- an encryption key.
    """

    simulate: str = SIMULATE_OK
    """Recorded at create time because ``destroy`` never sees the original inputs.

    The teardown contract takes only ``external_name`` and ``provider_ref`` -- deliberately,
    since a real teardown must work from what was persisted rather than from a request that
    may be long gone. So a requester who asked for a destroy failure has to have that wish
    stored on the resource itself, exactly as a real provider stores deletion protection.
    """


# --------------------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------------------


@runtime_checkable
class ResourceStore(Protocol):
    """Where a :class:`MemoryProvider` keeps what "exists"."""

    shared: bool
    """Whether other processes see the same resources. Reported by ``bailment sandbox``."""

    async def get(self, external_name: str) -> MemoryResource | None: ...

    async def outputs(self, external_name: str) -> dict[str, str]:
        """The credential minted for this resource, opened. Empty if it has none."""
        ...

    async def add(self, resource: MemoryResource) -> bool:
        """Insert. ``False`` when the name is already taken, which the caller adopts."""
        ...

    async def remove(self, external_name: str) -> bool: ...

    async def set_simulate(self, external_name: str | None, *, current: str, new: str) -> int:
        """Change ``simulate`` from ``current`` to ``new``; ``None`` means every resource."""
        ...

    async def all(self) -> list[MemoryResource]: ...

    async def clear(self) -> None: ...


class InProcessStore:
    """A dict. Correct for tests and for a single process that does everything."""

    shared = False

    def __init__(self) -> None:
        self.resources: dict[str, MemoryResource] = {}

    async def get(self, external_name: str) -> MemoryResource | None:
        return self.resources.get(external_name)

    async def outputs(self, external_name: str) -> dict[str, str]:
        resource = self.resources.get(external_name)
        return dict(resource.outputs) if resource is not None else {}

    async def add(self, resource: MemoryResource) -> bool:
        if resource.external_name in self.resources:
            return False
        self.resources[resource.external_name] = resource
        return True

    async def remove(self, external_name: str) -> bool:
        return self.resources.pop(external_name, None) is not None

    async def set_simulate(self, external_name: str | None, *, current: str, new: str) -> int:
        targets = (
            list(self.resources.values())
            if external_name is None
            else [r for r in (self.resources.get(external_name),) if r is not None]
        )
        changed = 0
        for resource in targets:
            if resource.simulate == current:
                resource.simulate = new
                changed += 1
        return changed

    async def all(self) -> list[MemoryResource]:
        return [self.resources[name] for name in sorted(self.resources)]

    async def clear(self) -> None:
        self.resources.clear()


def _process_sessionmaker() -> async_sessionmaker[AsyncSession]:
    from bailment.db import get_sessionmaker

    return get_sessionmaker()


def _process_secret_box() -> SecretBox:
    from bailment.config import get_settings
    from bailment.secrets import SecretBox

    return SecretBox.from_settings(get_settings())


class SqlResourceStore:
    """The ``memory_resources`` table. What every bailment process can see.

    Both dependencies are resolved on every call rather than captured at construction, so
    the store follows :func:`bailment.db.get_sessionmaker` across an engine being disposed
    and rebuilt -- which the CLI does after every command -- and so constructing the
    process-wide provider never opens a connection or demands a key. ``bailment keygen``
    imports the registry and has neither.

    Every database error is re-raised as a retryable :class:`ProviderError`. The provider
    contract says an unreachable resource is UNKNOWN and never GONE; a store that let a
    ``SQLAlchemyError`` out of ``exists`` would at least not lie, but a typed, retryable
    error is what the worker and the reconciler already know how to back off from.
    """

    shared = True

    def __init__(
        self,
        provider: str = "memory",
        *,
        sessionmaker: Callable[[], async_sessionmaker[AsyncSession]] = _process_sessionmaker,
        secret_box: Callable[[], SecretBox] = _process_secret_box,
    ) -> None:
        self.provider = provider
        self._sessionmaker = sessionmaker
        self._secret_box = secret_box

    def _unreachable(self, operation: str, exc: SQLAlchemyError) -> ProviderError:
        return ProviderError(
            self.provider,
            f"the shared sandbox store could not be reached ({type(exc).__name__}); what "
            f"exists at the memory provider is unknown until it answers",
            retryable=True,
            operation=operation,
        )

    def _to_resource(self, row: MemoryResourceRecord) -> MemoryResource:
        created = row.created_at
        if created.tzinfo is None:  # SQLite hands timestamps back naive; see service.aware
            created = created.replace(tzinfo=UTC)
        return MemoryResource(
            external_name=row.external_name,
            resource_id=row.resource_id,
            inputs=dict(row.inputs or {}),
            created_at=created,
            simulate=row.simulate,
        )

    async def get(self, external_name: str) -> MemoryResource | None:
        try:
            async with self._sessionmaker()() as session:
                row = await session.get(MemoryResourceRecord, (self.provider, external_name))
                return self._to_resource(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise self._unreachable("exists", exc) from None

    async def outputs(self, external_name: str) -> dict[str, str]:
        try:
            async with self._sessionmaker()() as session:
                row = await session.get(MemoryResourceRecord, (self.provider, external_name))
                sealed = row.sealed_outputs if row is not None else None
        except SQLAlchemyError as exc:
            raise self._unreachable("create", exc) from None
        return self._secret_box().open(sealed) if sealed else {}

    async def add(self, resource: MemoryResource) -> bool:
        # Sealed before a connection is opened: a missing key should fail the create
        # cleanly, not half-way through a transaction.
        sealed = self._secret_box().seal(resource.outputs) if resource.outputs else None
        try:
            async with self._sessionmaker()() as session:
                session.add(
                    MemoryResourceRecord(
                        provider=self.provider,
                        external_name=resource.external_name,
                        resource_id=resource.resource_id,
                        inputs=dict(resource.inputs),
                        simulate=resource.simulate,
                        sealed_outputs=sealed,
                        created_at=resource.created_at,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    # Another process created it first. The caller adopts, exactly as it
                    # would a resource a real provider already had under this name.
                    await session.rollback()
                    return False
        except SQLAlchemyError as exc:
            raise self._unreachable("create", exc) from None
        return True

    async def remove(self, external_name: str) -> bool:
        try:
            async with self._sessionmaker()() as session:
                result = await session.execute(
                    delete(MemoryResourceRecord).where(
                        MemoryResourceRecord.provider == self.provider,
                        MemoryResourceRecord.external_name == external_name,
                    )
                )
                await session.commit()
        except SQLAlchemyError as exc:
            raise self._unreachable("destroy", exc) from None
        return bool(getattr(result, "rowcount", 0))

    async def set_simulate(self, external_name: str | None, *, current: str, new: str) -> int:
        statement = (
            update(MemoryResourceRecord)
            .where(
                MemoryResourceRecord.provider == self.provider,
                MemoryResourceRecord.simulate == current,
            )
            .values(simulate=new)
        )
        if external_name is not None:
            statement = statement.where(MemoryResourceRecord.external_name == external_name)
        try:
            async with self._sessionmaker()() as session:
                result = await session.execute(statement)
                await session.commit()
        except SQLAlchemyError as exc:
            raise self._unreachable("relent", exc) from None
        return int(getattr(result, "rowcount", 0) or 0)

    async def all(self) -> list[MemoryResource]:
        try:
            async with self._sessionmaker()() as session:
                rows = await session.execute(
                    select(MemoryResourceRecord)
                    .where(MemoryResourceRecord.provider == self.provider)
                    .order_by(MemoryResourceRecord.external_name)
                )
                return [self._to_resource(row) for row in rows.scalars().all()]
        except SQLAlchemyError as exc:
            raise self._unreachable("list_managed", exc) from None

    async def clear(self) -> None:
        try:
            async with self._sessionmaker()() as session:
                await session.execute(
                    delete(MemoryResourceRecord).where(
                        MemoryResourceRecord.provider == self.provider
                    )
                )
                await session.commit()
        except SQLAlchemyError as exc:
            raise self._unreachable("reset", exc) from None


# --------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Fault:
    message: str
    retryable: bool
    half_succeed: bool


class MemoryProvider:
    """A complete provider over a :class:`ResourceStore`, with hooks to force failures."""

    supports_reconciliation = True

    def __init__(
        self,
        *,
        name: str = "memory",
        latency_range: tuple[float, float] = (0.005, 0.03),
        estimated_hourly_usd: float = 0.0,
        store: ResourceStore | None = None,
    ) -> None:
        self.name = name
        self.latency_range = latency_range
        self.estimated_hourly_usd = estimated_hourly_usd
        self.store: ResourceStore = store if store is not None else InProcessStore()

        self._faults: dict[Operation, deque[_Fault]] = defaultdict(deque)
        self._forced_status: dict[str, ResourceStatus] = {}
        self._unavailable_reason: str | None = None
        self._pending_half_success: _Fault | None = None
        self._lock = asyncio.Lock()

        self.calls: dict[Operation, int] = defaultdict(int)
        """Call counts, so a test can assert a retry did not become a second create."""

    # -- availability ---------------------------------------------------------------

    def is_available(self) -> bool:
        return self._unavailable_reason is None

    def availability_reason(self) -> str | None:
        return self._unavailable_reason

    def set_unavailable(self, reason: str | None) -> None:
        """Simulate a provider whose credentials are missing, for registry tests."""
        self._unavailable_reason = reason

    # -- fault hooks, process-local ---------------------------------------------------

    def fail_next(
        self,
        operation: Operation,
        *,
        message: str = "injected failure",
        retryable: bool = False,
        half_succeed: bool = False,
    ) -> None:
        """Arm one failure for the next call to ``operation``.

        Faults queue, so arming twice fails the next two calls. ``half_succeed`` only
        means anything for ``create``: the resource is stored *and then* the call raises,
        which is the shape of every real half-completed provision.
        """
        self._faults[operation].append(
            _Fault(message=message, retryable=retryable, half_succeed=half_succeed)
        )

    def force_status(self, external_name: str, status: ResourceStatus | None) -> None:
        """Pin what ``exists`` reports for one name; ``None`` clears it.

        Pin ``UNKNOWN`` to check that nothing downstream quietly rounds it to GONE.
        """
        if status is None:
            self._forced_status.pop(external_name, None)
        else:
            self._forced_status[external_name] = status

    # -- drift hooks, on whatever store this provider uses ----------------------------
    #
    # The ``a``-prefixed methods work on any store and are what ``bailment sandbox`` calls.
    # The unprefixed ones are synchronous conveniences for the in-process store, kept
    # because a test that wants to vanish a resource should not have to become a
    # coroutine to do it; against a shared store they refuse and name the async variant.

    async def arelent(self, external_name: str | None = None) -> int:
        """Clear ``destroy_failure`` so a stuck teardown can finally succeed.

        Stands in for the out-of-band fix that ends the real version of this story: the
        deletion protection someone turns off, the dependent resource they finally remove,
        the quota that gets raised. Without it a simulated orphan is permanent, and the
        demo has no ending -- you would be left with a lease that can never be resolved by
        any action available in the product, which is a worse lesson than the one intended.
        ``bailment sandbox relent`` calls this; ``bailment lease retry-teardown`` then
        finishes the job.

        Returns how many resources were changed. Pass a name to relent for one, or nothing
        to relent for all of them.
        """
        return await self.store.set_simulate(
            external_name, current=SIMULATE_DESTROY_FAILURE, new=SIMULATE_OK
        )

    async def avanish(self, external_name: str) -> bool:
        """Delete a resource behind bailment's back, as a human with console access would.

        Returns whether anything was removed. The lease row is untouched on purpose --
        that mismatch is the drift the reconciler has to notice.
        """
        self._forced_status.pop(external_name, None)
        return await self.store.remove(external_name)

    async def aplant_orphan(
        self, external_name: str, *, age: timedelta = timedelta(0), **inputs: Any
    ) -> MemoryResource:
        """Create a resource that no lease has ever heard of.

        Proves the orphan sweep finds resources rather than merely agreeing with the
        database about the ones it already knows. ``age`` backdates it, standing in for a
        worker that died a while ago rather than one that is mid-provision -- which is the
        distinction the reconciler's grace period exists to draw.
        """
        resource = MemoryResource(
            external_name=external_name,
            resource_id=f"mem_{secrets.token_hex(8)}",
            inputs=dict(inputs),
            created_at=utcnow() - age,
        )
        if not await self.store.add(resource):
            existing = await self.store.get(external_name)
            if existing is not None:
                return existing
        return resource

    async def asnapshot(self) -> dict[str, MemoryResource]:
        """What currently exists, by name. Credentials are not opened."""
        return {resource.external_name: resource for resource in await self.store.all()}

    async def areset(self) -> None:
        await self.store.clear()
        self._reset_local()

    def _local(self, hook: str) -> InProcessStore:
        if isinstance(self.store, InProcessStore):
            return self.store
        raise RuntimeError(
            f"{hook}() only works on an in-process store; this provider shares its "
            f"resources through {type(self.store).__name__}, so use 'await "
            f"provider.a{hook}(...)' or the 'bailment sandbox' command instead"
        )

    @property
    def _resources(self) -> dict[str, MemoryResource]:
        """The in-process dict, for tests that reach into it to backdate a resource."""
        return self._local("snapshot").resources

    def relent(self, external_name: str | None = None) -> int:
        """Synchronous :meth:`arelent`, for the in-process store."""
        store = self._local("relent")
        targets = (
            list(store.resources.values())
            if external_name is None
            else [r for r in (store.resources.get(external_name),) if r is not None]
        )
        changed = 0
        for resource in targets:
            if resource.simulate == SIMULATE_DESTROY_FAILURE:
                resource.simulate = SIMULATE_OK
                changed += 1
        return changed

    def vanish(self, external_name: str) -> bool:
        """Synchronous :meth:`avanish`, for the in-process store."""
        self._forced_status.pop(external_name, None)
        return self._local("vanish").resources.pop(external_name, None) is not None

    def plant_orphan(self, external_name: str, **inputs: Any) -> MemoryResource:
        """Synchronous :meth:`aplant_orphan`, for the in-process store."""
        resource = MemoryResource(
            external_name=external_name,
            resource_id=f"mem_{secrets.token_hex(8)}",
            inputs=dict(inputs),
        )
        self._local("plant_orphan").resources[external_name] = resource
        return resource

    def snapshot(self) -> dict[str, MemoryResource]:
        """A copy of what currently exists. Read-only view for assertions and the demo."""
        return dict(self._local("snapshot").resources)

    def reset(self) -> None:
        self._local("reset").resources.clear()
        self._reset_local()

    def _reset_local(self) -> None:
        self._faults.clear()
        self._forced_status.clear()
        self._unavailable_reason = None
        self._pending_half_success = None
        self.calls.clear()

    # -- internals ------------------------------------------------------------------

    async def _tick(self, operation: Operation) -> None:
        """Count the call, sleep a plausible amount, then fire any armed fault."""
        self.calls[operation] += 1
        low, high = self.latency_range
        if high > 0:
            # Jitter only exists to keep tests honest about concurrency; not crypto.
            await asyncio.sleep(random.uniform(low, high))  # noqa: S311
        queue = self._faults.get(operation)
        if queue:
            fault = queue.popleft()
            if not fault.half_succeed:
                raise ProviderError(
                    self.name, fault.message, retryable=fault.retryable, operation=operation
                )
            self._pending_half_success = fault

    def _take_half_success(self) -> _Fault | None:
        fault = self._pending_half_success
        self._pending_half_success = None
        return fault

    def _mint_outputs(
        self, external_name: str, declared_outputs: Sequence[str] = ()
    ) -> dict[str, str]:
        """Synthesise a plausible value for every output the golden path declares.

        Naming them from the path rather than from this file is the whole point. A fake
        provider that returns a fixed pair of keys only ever satisfies the one golden
        path it was written next to; every other path built on it gets its outputs
        rejected as undeclared by the worker and reaches ACTIVE with nothing to show.
        That failure is quiet, appears only at runtime, and is exactly the kind of drift
        deriving everything from one definition is supposed to prevent.

        The shape of each value is inferred from its name so the sample looks like the
        thing it stands for -- a reader comparing ``SANDBOX_URL`` against a real
        ``DATABASE_URL`` should not be able to tell the difference structurally.
        """
        token = secrets.token_urlsafe(24)
        names = list(declared_outputs) or ["DATABASE_URL", "API_TOKEN"]

        outputs: dict[str, str] = {}
        for name in names:
            upper = name.upper()
            if upper.endswith(("_URL", "_URI", "_DSN")):
                outputs[name] = f"memory://{external_name}:{token}@127.0.0.1:0/{external_name}"
            elif upper.endswith(("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")):
                outputs[name] = token
            elif upper.endswith(("_ID", "_NAME")):
                outputs[name] = external_name
            elif upper.endswith(("_HOST", "_FQDN")):
                outputs[name] = f"{external_name}.memory.invalid"
            elif upper.endswith("_PORT"):
                outputs[name] = "0"
            else:
                outputs[name] = f"{external_name}-{name.lower()}"
        return outputs

    def _simulation(self, inputs: dict[str, Any]) -> str:
        """Read and validate the requested ``simulate`` behaviour.

        Rejects an unknown value rather than quietly treating it as ``ok``. A demo of the
        failure paths that silently succeeds because of a typo teaches the opposite of what
        it set out to.
        """
        requested = inputs.get("simulate", SIMULATE_OK)
        if not isinstance(requested, str) or requested not in SIMULATIONS:
            raise ProviderError(
                self.name,
                f"unknown simulate value {requested!r}; expected one of "
                f"{', '.join(sorted(SIMULATIONS))}",
                retryable=False,
                operation="preflight",
            )
        return requested

    def _to_managed(self, resource: MemoryResource) -> ManagedResource:
        return ManagedResource(
            external_name=resource.external_name,
            provider_resource_id=resource.resource_id,
            provider_ref={"resource_id": resource.resource_id, "name": resource.external_name},
            created_at=resource.created_at,
            detail={"inputs": resource.inputs},
        )

    async def _adopt(self, existing: MemoryResource) -> ProvisionResult:
        # The idempotent path. A retry after a lost response must not double provision,
        # and must return the credential the first call minted -- a fresh one would leave
        # the caller holding a secret for a resource that no longer accepts it.
        return ProvisionResult(
            provider_resource_id=existing.resource_id,
            provider_ref={"resource_id": existing.resource_id, "name": existing.external_name},
            outputs=await self.store.outputs(existing.external_name),
            estimated_hourly_usd=self.estimated_hourly_usd,
            adopted=True,
            detail={"created_at": existing.created_at.isoformat()},
        )

    # -- the contract ---------------------------------------------------------------

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None:
        require_managed_name(self.name, external_name, operation="preflight")
        # Exercised by the demo's "bad input" path; also keeps the fake honest about
        # rejecting garbage before anything is named.
        coerce_str(self.name, inputs, "note")
        self._simulation(inputs)
        await self._tick("preflight")

    async def create(
        self,
        *,
        external_name: str,
        inputs: dict[str, Any],
        declared_outputs: Sequence[str] = (),
    ) -> ProvisionResult:
        require_managed_name(self.name, external_name, operation="create")
        async with self._lock:
            await self._tick("create")
            existing = await self.store.get(external_name)
            if existing is not None:
                return await self._adopt(existing)

            simulate = self._simulation(inputs)
            if simulate == SIMULATE_SLOW_CREATE:
                await asyncio.sleep(SLOW_CREATE_SECONDS)
            if simulate == SIMULATE_CREATE_FAILURE:
                # Raised before the resource is recorded, so this is the clean failure:
                # nothing was created, there is nothing to roll back, and the lease is
                # entitled to reach FAILED rather than ORPHANED. `fail_next(half_succeed=True)`
                # is how you get the dirty version.
                raise ProviderError(
                    self.name,
                    "simulated create failure, as requested by simulate=create_failure",
                    retryable=False,
                    operation="create",
                )

            resource = MemoryResource(
                external_name=external_name,
                resource_id=f"mem_{secrets.token_hex(8)}",
                inputs=dict(inputs),
                outputs=self._mint_outputs(external_name, declared_outputs),
                simulate=simulate,
            )
            if not await self.store.add(resource):
                # Somebody else's create landed between our look and our write -- another
                # process, on a shared store. Theirs is the resource; adopt it.
                raced = await self.store.get(external_name)
                if raced is not None:
                    return await self._adopt(raced)

            half = self._take_half_success()
            if half is not None:
                # Stored, then failed. From the engine's side this is indistinguishable
                # from a worker dying after the provider committed -- which is the point.
                raise ProviderError(
                    self.name, half.message, retryable=half.retryable, operation="create"
                )

            return ProvisionResult(
                provider_resource_id=resource.resource_id,
                provider_ref={"resource_id": resource.resource_id, "name": external_name},
                outputs=dict(resource.outputs),
                estimated_hourly_usd=self.estimated_hourly_usd,
                detail={"created_at": resource.created_at.isoformat()},
            )

    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None:
        async with self._lock:
            await self._tick("destroy")

            existing = await self.store.get(external_name)
            if existing is not None and existing.simulate == SIMULATE_DESTROY_FAILURE:
                # Raise WITHOUT removing the resource. That ordering is the entire point:
                # the thing is still there, still costing money in the real-provider
                # version of this story, and the lease row is about to stop claiming
                # responsibility for it. Popping first and then raising would produce an
                # ORPHANED lease pointing at nothing, which is a tidier demo and a
                # dishonest one -- the reconciler would sweep and find the account clean.
                raise ProviderError(
                    self.name,
                    "simulated teardown failure, as requested by simulate=destroy_failure; "
                    "the resource is still present. 'bailment sandbox relent' lifts it",
                    retryable=True,
                    operation="destroy",
                )

            # Deleting something already gone is success. Teardown gets retried, and a
            # retry that errors on the second pass can never reach RELEASED.
            await self.store.remove(external_name)
            self._forced_status.pop(external_name, None)

    async def exists(self, *, external_name: str, provider_ref: dict[str, Any]) -> ResourceStatus:
        await self._tick("exists")
        forced = self._forced_status.get(external_name)
        if forced is not None:
            return forced
        found = await self.store.get(external_name)
        return ResourceStatus.EXISTS if found is not None else ResourceStatus.GONE

    async def list_managed(self) -> list[ManagedResource]:
        await self._tick("list_managed")
        return [
            self._to_managed(resource)
            for resource in await self.store.all()
            # Same defensive filter the real providers apply. A planted orphan can carry
            # an unmanaged name, and the reconciler must never be shown one.
            if is_managed_name(resource.external_name)
        ]

    async def aclose(self) -> None:
        """No connections to close. Present so the registry can treat providers alike."""
        return None


_DEFAULT: MemoryProvider | None = None


def default_memory_provider() -> MemoryProvider:
    """The process-wide instance the registry hands out, backed by the shared store.

    A module-level singleton so that an API request, a worker tick and a reconciler pass
    running in the same process use one set of fault hooks. The resources themselves are in
    the database, so a process that is *not* this one sees them too; see the module
    docstring for why that is the whole point. Tests that want isolation construct their
    own :class:`MemoryProvider`, which defaults to an in-process store.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = MemoryProvider(store=SqlResourceStore())
    return _DEFAULT

