"""An in-process provider that behaves like a real one, including badly.

This is not a stub. It creates resources, hands back credentials, refuses to
double-provision on retry, distinguishes a missing resource from an unreachable one, and
enumerates only the resources carrying bailment's marker -- the same contract the Neon,
Upstash and Cloudflare providers meet against real APIs.

What it adds is the ability to *choose* the failure. Every interesting property of this
system is a property of its failure paths, and those paths are exactly the ones you
cannot trigger on demand against a real cloud API:

* :meth:`fail_next` with ``half_succeed=True`` -- the create call that really does create
  the resource and then fails to report it. The lease ends up FAILED while the resource
  bills. This is the orphan the reconciler exists to find, and without a hook like this
  it can only be produced by killing a worker at the right microsecond.
* :meth:`fail_next` on ``destroy`` -- teardown that does not tear down, which must drive
  the lease to ORPHANED and never to RELEASED.
* :meth:`vanish` -- a resource deleted out of band, by a human in a console. The lease
  still says ACTIVE. This is drift in the other direction.
* :meth:`force_status` -- a resource whose status cannot be determined, so ``exists``
  answers UNKNOWN and the system has to prove it does not treat that as GONE.

State is process-local, so the API, the worker and the reconciler only see the same
resources when they run in one process. That is fine for tests and the demo and would be
wrong for anything else; there is a real provider for anything else.
"""

from __future__ import annotations

import asyncio
import random
import secrets
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bailment.models import utcnow
from bailment.providers.base import (
    ManagedResource,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
    coerce_str,
    is_managed_name,
    require_managed_name,
)

__all__ = ["MemoryProvider", "MemoryResource", "default_memory_provider"]

Operation = str  # one of: preflight, create, destroy, exists, list_managed

#: Behaviours a caller can ask for through the ``simulate`` input.
#:
#: :meth:`fail_next` arms a fault out of band, which is what tests want. This is the other
#: half: a fault a *requester* selects, declared in the golden path's input schema, so the
#: failure paths can be walked through from the dashboard or an MCP tool by somebody who is
#: evaluating bailment and has no intention of writing a test.
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
    """Held so an adopted retry returns the same credential rather than a fresh one."""

    simulate: str = SIMULATE_OK
    """Recorded at create time because ``destroy`` never sees the original inputs.

    The teardown contract takes only ``external_name`` and ``provider_ref`` -- deliberately,
    since a real teardown must work from what was persisted rather than from a request that
    may be long gone. So a requester who asked for a destroy failure has to have that wish
    stored on the resource itself, exactly as a real provider stores deletion protection.
    """


@dataclass(slots=True)
class _Fault:
    message: str
    retryable: bool
    half_succeed: bool


class MemoryProvider:
    """A complete provider backed by a dict, with hooks to force failures."""

    supports_reconciliation = True

    def __init__(
        self,
        *,
        name: str = "memory",
        latency_range: tuple[float, float] = (0.005, 0.03),
        estimated_hourly_usd: float = 0.0,
    ) -> None:
        self.name = name
        self.latency_range = latency_range
        self.estimated_hourly_usd = estimated_hourly_usd

        self._resources: dict[str, MemoryResource] = {}
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

    # -- test / demo hooks ----------------------------------------------------------

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

    def relent(self, external_name: str | None = None) -> int:
        """Clear ``destroy_failure`` so a stuck teardown can finally succeed.

        Stands in for the out-of-band fix that ends the real version of this story: the
        deletion protection someone turns off, the dependent resource they finally remove,
        the quota that gets raised. Without it a simulated orphan is permanent, and the
        demo has no ending -- you would be left with a lease that can never be resolved by
        any action available in the product, which is a worse lesson than the one intended.

        Returns how many resources were changed. Pass a name to relent for one, or nothing
        to relent for all of them.
        """
        targets = (
            [self._resources[external_name]]
            if external_name is not None and external_name in self._resources
            else list(self._resources.values())
            if external_name is None
            else []
        )
        changed = 0
        for resource in targets:
            if resource.simulate == SIMULATE_DESTROY_FAILURE:
                resource.simulate = SIMULATE_OK
                changed += 1
        return changed

    def vanish(self, external_name: str) -> bool:
        """Delete a resource behind bailment's back, as a human with console access would.

        Returns whether anything was removed. The lease row is untouched on purpose --
        that mismatch is the drift the reconciler has to notice.
        """
        return self._resources.pop(external_name, None) is not None

    def plant_orphan(self, external_name: str, **inputs: Any) -> MemoryResource:
        """Create a resource that no lease has ever heard of.

        Used to prove the orphan sweep finds resources rather than merely agreeing with
        the database about the ones it already knows.
        """
        resource = MemoryResource(
            external_name=external_name,
            resource_id=f"mem_{secrets.token_hex(8)}",
            inputs=dict(inputs),
        )
        self._resources[external_name] = resource
        return resource

    def force_status(self, external_name: str, status: ResourceStatus | None) -> None:
        """Pin what ``exists`` reports for one name; ``None`` clears it.

        Pin ``UNKNOWN`` to check that nothing downstream quietly rounds it to GONE.
        """
        if status is None:
            self._forced_status.pop(external_name, None)
        else:
            self._forced_status[external_name] = status

    def reset(self) -> None:
        self._resources.clear()
        self._faults.clear()
        self._forced_status.clear()
        self._unavailable_reason = None
        self._pending_half_success = None
        self.calls.clear()

    def snapshot(self) -> dict[str, MemoryResource]:
        """A copy of what currently exists. Read-only view for assertions and the demo."""
        return dict(self._resources)

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
            existing = self._resources.get(external_name)
            if existing is not None:
                # The idempotent path. A retry after a lost response must not double
                # provision, and must return the credential the first call minted --
                # a fresh one would leave the caller holding a secret for a resource
                # that no longer accepts it.
                result = ProvisionResult(
                    provider_resource_id=existing.resource_id,
                    provider_ref={"resource_id": existing.resource_id, "name": external_name},
                    outputs=dict(existing.outputs),
                    estimated_hourly_usd=self.estimated_hourly_usd,
                    adopted=True,
                    detail={"created_at": existing.created_at.isoformat()},
                )
                return result

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
            self._resources[external_name] = resource

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

            existing = self._resources.get(external_name)
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
                    "the resource is still present",
                    retryable=True,
                    operation="destroy",
                )

            # Deleting something already gone is success. Teardown gets retried, and a
            # retry that errors on the second pass can never reach RELEASED.
            self._resources.pop(external_name, None)
            self._forced_status.pop(external_name, None)

    async def exists(self, *, external_name: str, provider_ref: dict[str, Any]) -> ResourceStatus:
        await self._tick("exists")
        forced = self._forced_status.get(external_name)
        if forced is not None:
            return forced
        return ResourceStatus.EXISTS if external_name in self._resources else ResourceStatus.GONE

    async def list_managed(self) -> list[ManagedResource]:
        await self._tick("list_managed")
        return [
            self._to_managed(resource)
            for name, resource in sorted(self._resources.items())
            # Same defensive filter the real providers apply. plant_orphan can be handed
            # an unmanaged name, and the reconciler must never be shown one.
            if is_managed_name(name)
        ]

    async def aclose(self) -> None:
        """No connections to close. Present so the registry can treat providers alike."""
        return None


_DEFAULT: MemoryProvider | None = None


def default_memory_provider() -> MemoryProvider:
    """The process-wide instance the registry hands out.

    A module-level singleton so that an API request, a worker tick and a reconciler pass
    running in the same process act on the same fake cloud. Tests that want isolation
    construct their own :class:`MemoryProvider`.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = MemoryProvider()
    return _DEFAULT
