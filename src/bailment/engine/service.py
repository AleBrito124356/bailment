"""The request and lease service layer -- the one door into the engine.

The REST API, the MCP server, the OSB endpoints and the CLI all call this module and
none of them talk to :mod:`bailment.models` directly. That is not tidiness. There are
four invariants that only hold if every caller goes through the same code:

**Nothing reaches a provider until the row that names it is durable.**
:meth:`LeaseService.request_provision` computes ``external_name`` and commits it, and
only then is the lease visible to a worker that could call a provider. An API handler
that built its own lease row and forgot the commit boundary would open exactly the
window write-ahead naming exists to close -- a real resource with no row that names it.

**A secret value leaves the system through exactly one function.**
:meth:`LeaseService.resolve_binding` is the only code path in bailment that decrypts a
binding, and it refuses any caller that is not the CLI injection path or a human
operator. Every other method returns a ``bailment://binding/...`` reference. The check is
an explicit :class:`CallerKind` argument rather than a convention, because a convention
is a thing a future MCP tool handler can satisfy by accident.

**Non-secret outputs are published without decrypting anything.** A golden path may
declare an output ``secret: false`` -- ``FQDN`` on the DNS path, ``SANDBOX_ID`` on the
sandbox. Those values still go into the sealed envelope with everything else, because
:mod:`bailment.secrets` seals a binding as one unit on purpose, but the worker *also*
writes the non-secret subset into the ``activated`` audit event in plain text. Reads
therefore never open the envelope. The alternative -- decrypting on every ``get_lease``
to filter out the secret keys -- would turn the most-called method in the service into a
decryption path, and the first refactor that forgot the filter would be a disclosure.

**Policy is evaluated against what will actually happen.** TTL is clamped to the golden
path's ceiling *before* the policy context is built, so a rule reading ``ttl_seconds``
compares against the granted duration and not the requested one. Clamping rather than
rejecting is itself deliberate: a rejected request teaches an agent nothing and it
retries with another guess, while a clamped one comes back with ``max_ttl_seconds`` in
the response and the agent stops guessing.

----

**Where the clock starts.** ``expires_at`` is set when the lease goes ACTIVE, not when it
is requested. A lease that spends three hours in AWAITING_APPROVAL has not been holding
a resource for three hours, and burning the TTL while waiting for a human would hand the
approver a lease that expires before the requester can use it.

**Mutating methods commit; readers do not.** This departs from the "endpoints commit"
convention in :mod:`bailment.db` for one reason: the durability of a lease row is part of
its meaning. A lease that exists only inside an uncommitted transaction is a lease no
worker can claim and no reconciler can match a resource against, so the service does not
leave that decision to its caller.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bailment.catalog.loader import Catalog, UnknownGoldenPath
from bailment.catalog.schema import GoldenPath, format_duration, parse_duration
from bailment.config import Settings, get_settings
from bailment.engine.validation import InputValidationError, SchemaError, validate_inputs
from bailment.logging import get_logger
from bailment.models import Approval, AuditEvent, Binding, Lease, new_id, utcnow
from bailment.policy.engine import PolicyDecision, PolicyEngine, RequestContext
from bailment.providers.base import require_managed_name, resource_prefix
from bailment.providers.registry import ProviderRegistry
from bailment.secrets import DecryptionError, SecretBox, make_reference, parse_reference
from bailment.states import (
    LIVE_STATES,
    TEARDOWN_INTENT_STATES,
    TERMINAL_STATES,
    USABLE_STATES,
    IllegalTransition,
    LeaseState,
    assert_transition,
)

__all__ = [
    "ACTION_ACTIVATED",
    "DEFAULT_APPROVAL_WINDOW",
    "MIN_TTL_SECONDS",
    "ApprovalView",
    "BindingNotFound",
    "Caller",
    "CallerKind",
    "ConflictingState",
    "InvalidRequest",
    "LeaseNotFound",
    "LeaseService",
    "LeaseView",
    "NotAuthorized",
    "PathDisabled",
    "ProvisionOutcome",
    "ProvisionRequest",
    "SecretAccessDenied",
    "ServiceError",
    "UnknownPath",
    "aware",
    "backoff_seconds",
    "claim_is_stale",
    "clamp_ttl",
    "make_external_name",
    "public_output_names",
    "published_outputs",
    "queue_for_provisioning",
    "queue_for_teardown",
    "record_event",
    "seconds_remaining",
    "transition",
]

log = get_logger("bailment.engine.service")


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: A lease shorter than this cannot be used for anything. The lease ticker runs every
#: ``lease_tick_seconds`` (15 by default), so a 10 second lease would spend most of its
#: life waiting to be noticed. Requests below the floor are clamped up to it, for the
#: same reason requests above the ceiling are clamped down: the caller learns the bound
#: from the response instead of from a rejection.
MIN_TTL_SECONDS: Final = 60

#: How long an approval request waits before the lease ticker gives up on it. Not a
#: setting because :mod:`bailment.config` is a fixed contract in this codebase; pass
#: ``approval_window`` to :class:`LeaseService` to override it per deployment.
DEFAULT_APPROVAL_WINDOW: Final = timedelta(hours=24)

#: Longest name a provider will accept, per
#: :func:`bailment.providers.base.require_managed_name`.
_NAME_LIMIT: Final = 63

#: Characters legal in the golden-path slug half of an external name.
_SLUG_UNSAFE: Final = re.compile(r"[^a-z0-9._-]+")
_HEX: Final = re.compile(r"[^0-9a-f]")

#: Relationships :meth:`LeaseService._view` reads. Listed so a freshly created lease can
#: be refreshed in one statement instead of tripping three separate lazy loads.
_VIEW_RELATIONSHIPS: Final[tuple[str, ...]] = ("events", "bindings", "approval")

# Audit actions. Constants rather than string literals at the call sites because the
# dashboard filters on them and a typo would produce an event nobody ever sees again.
ACTION_REQUESTED: Final = "requested"
ACTION_REPLAYED: Final = "idempotent_replay"
ACTION_POLICY_DENIED: Final = "policy_denied"
ACTION_APPROVAL_REQUESTED: Final = "approval_requested"
ACTION_APPROVED: Final = "approved"
ACTION_REJECTED: Final = "rejected"
ACTION_APPROVAL_EXPIRED: Final = "approval_expired"
ACTION_QUEUED: Final = "queued"
ACTION_CLAIMED: Final = "claimed"
ACTION_PROVISIONING: Final = "provisioning"
ACTION_ACTIVATED: Final = "activated"
ACTION_PROVISION_FAILED: Final = "provision_failed"
ACTION_ROLLED_BACK: Final = "rolled_back"
ACTION_WARNED: Final = "expiring"
ACTION_EXPIRED: Final = "expired"
ACTION_RENEWED: Final = "renewed"
ACTION_REVOKED: Final = "revoked"
ACTION_TEARDOWN: Final = "teardown_started"
ACTION_RELEASED: Final = "released"
ACTION_ORPHANED: Final = "orphaned"
ACTION_UNKNOWN_STATE: Final = "state_unknown"
ACTION_BINDING_RESOLVED: Final = "binding_resolved"
ACTION_BINDING_REVOKED: Final = "binding_revoked"
ACTION_RECONCILE_DRIFT: Final = "reconcile_drift"
ACTION_RECONCILE_ORPHAN: Final = "reconcile_orphan"


# --------------------------------------------------------------------------------------
# Callers
# --------------------------------------------------------------------------------------


class CallerKind(StrEnum):
    """Which surface a call arrived on.

    This is a security boundary, not bookkeeping. ``AGENT`` is the only value that can
    reach the MCP tools, and it is the value that :meth:`LeaseService.resolve_binding`
    and :meth:`LeaseService.approve` refuse outright -- an agent that can decrypt its own
    binding makes the whole reference indirection decorative, and an agent that can
    approve its own request makes ``require_approval`` decorative.
    """

    AGENT = "agent"
    """An AI agent over MCP. Never sees a secret value, never approves anything."""

    HUMAN = "human"
    """A person at the dashboard or calling the REST API with an ordinary token."""

    OPERATOR = "operator"
    """A person holding an admin token. May approve, revoke and read binding values."""

    CLI = "cli"
    """The local ``bailment run`` injection path, which puts values into a subprocess."""

    SYSTEM = "system"
    """The workers, the ticker and the reconciler. Cannot read binding values either."""


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling, and with what authority."""

    principal: str
    kind: CallerKind
    on_behalf_of: str | None = None
    """The human an agent is acting for. Recorded separately from ``principal`` so the
    audit log can answer "which agent" and "who is accountable" as different questions."""

    session: str | None = None
    """Opaque MCP session id, so one runaway agent session can be traced end to end."""

    @property
    def is_agent(self) -> bool:
        return self.kind is CallerKind.AGENT

    @property
    def is_operator(self) -> bool:
        return self.kind is CallerKind.OPERATOR

    @property
    def may_read_secrets(self) -> bool:
        """Only the CLI injection path and a human operator. See the module docstring."""
        return self.kind in (CallerKind.CLI, CallerKind.OPERATOR)

    @property
    def may_see_everything(self) -> bool:
        """Whether this caller may read leases belonging to other principals."""
        return self.kind in (CallerKind.OPERATOR, CallerKind.SYSTEM)

    @classmethod
    def agent(
        cls, principal: str, *, on_behalf_of: str | None = None, session: str | None = None
    ) -> Caller:
        return cls(principal, CallerKind.AGENT, on_behalf_of=on_behalf_of, session=session)

    @classmethod
    def human(cls, principal: str) -> Caller:
        return cls(principal, CallerKind.HUMAN)

    @classmethod
    def operator(cls, principal: str) -> Caller:
        return cls(principal, CallerKind.OPERATOR)

    @classmethod
    def cli(cls, principal: str) -> Caller:
        return cls(principal, CallerKind.CLI)

    @classmethod
    def system(cls, principal: str = "bailment") -> Caller:
        return cls(principal, CallerKind.SYSTEM)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ServiceError(Exception):
    """Base for everything this layer raises deliberately."""


class UnknownPath(ServiceError):
    """No golden path with that id. Maps to 404."""


class PathDisabled(ServiceError):
    """The golden path exists but is switched off. Also 404.

    Deliberately indistinguishable from "unknown" in status terms: a disabled path is not
    something a caller may provision, and telling an agent that a capability exists but is
    turned off invites it to keep retrying in case it comes back.
    """


class LeaseNotFound(ServiceError):
    """No such lease, or not one this caller may see. Maps to 404."""


class BindingNotFound(ServiceError):
    """No such binding reference. Maps to 404."""


class NotAuthorized(ServiceError):
    """This caller may not do this. Maps to 403."""


class SecretAccessDenied(NotAuthorized):
    """A caller that may never see plaintext asked for it.

    Its own type because this is the one denial worth alerting on: an agent reaching
    :meth:`LeaseService.resolve_binding` means some surface handed it the wrong caller
    kind, and that is a defect in bailment rather than a misbehaving agent.
    """


class InvalidRequest(ServiceError):
    """The request does not satisfy the golden path's schema. Maps to 400."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__(message)


class ConflictingState(ServiceError):
    """The lease is not in a state where this operation means anything. Maps to 409."""


class RenewalRefused(ConflictingState):
    """Renewal is not allowed, is exhausted, or the lease is past renewing."""


class ProviderUnavailable(ServiceError):
    """The golden path names a provider that is not configured. Maps to 503."""


# --------------------------------------------------------------------------------------
# Views -- everything below is safe to serialise to an agent
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalView:
    id: str
    reason: str
    allowed_approvers: tuple[str, ...]
    requested_at: datetime
    deadline_at: datetime | None
    decided_at: datetime | None
    decided_by: str | None
    approved: bool | None
    decision_note: str | None
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "allowed_approvers": list(self.allowed_approvers),
            "requested_at": self.requested_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "approved": self.approved,
            "decision_note": self.decision_note,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class LeaseView:
    """A lease as any caller may see it.

    There is one view and not an agent view plus a human view, because two renderings of
    the same row is how a field ends up in the wrong one. Everything here is safe to hand
    an agent; anything that is not safe simply is not a field.
    """

    id: str
    golden_path_id: str
    provider: str
    state: LeaseState
    requester: str
    on_behalf_of: str | None
    inputs: dict[str, Any]

    created_at: datetime
    activated_at: datetime | None
    expires_at: datetime | None
    released_at: datetime | None
    seconds_remaining: int | None

    ttl_seconds: int
    max_ttl_seconds: int
    renewals: int
    max_renewals: int
    renewable: bool

    policy_effect: str | None
    policy_reason: str | None

    external_name: str | None
    estimated_hourly_usd: float

    binding_reference: str | None
    outputs: dict[str, str]
    """Only the values the golden path declares ``secret: false``. Never a credential."""

    secret_output_names: tuple[str, ...]
    """Which sealed values exist, by name, so a consumer knows the shape of what it has."""

    approval: ApprovalView | None
    failure_reason: str | None

    @property
    def usable(self) -> bool:
        return self.state in USABLE_STATES

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "golden_path": self.golden_path_id,
            "provider": self.provider,
            "state": self.state.value,
            "requester": self.requester,
            "on_behalf_of": self.on_behalf_of,
            "inputs": dict(self.inputs),
            "created_at": self.created_at.isoformat(),
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "seconds_remaining": self.seconds_remaining,
            "ttl_seconds": self.ttl_seconds,
            "max_ttl_seconds": self.max_ttl_seconds,
            "renewals": self.renewals,
            "max_renewals": self.max_renewals,
            "renewable": self.renewable,
            "policy_effect": self.policy_effect,
            "policy_reason": self.policy_reason,
            "external_name": self.external_name,
            "estimated_hourly_usd": self.estimated_hourly_usd,
            "binding_reference": self.binding_reference,
            "outputs": dict(self.outputs),
            "secret_output_names": list(self.secret_output_names),
            "approval": self.approval.as_dict() if self.approval else None,
            "failure_reason": self.failure_reason,
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class ProvisionRequest:
    """What a caller asked for, before anything has been decided about it."""

    golden_path_id: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    ttl: str | None = None
    """Compact duration such as ``2h``. May also arrive inside ``inputs`` as ``ttl``,
    because :meth:`GoldenPath.input_schema_with_lease` advertises it there to agents."""

    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ProvisionOutcome:
    """The answer to a provision request.

    ``notices`` is the channel that teaches a caller its bounds. A clamped TTL puts a
    sentence in here naming the ceiling, and an agent that reads it stops asking for 30
    day databases without anybody having to write a rejection message.
    """

    lease: LeaseView
    replayed: bool = False
    ttl_clamped: bool = False
    notices: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "lease": self.lease.as_dict(),
            "replayed": self.replayed,
            "ttl_clamped": self.ttl_clamped,
            "notices": list(self.notices),
        }


# --------------------------------------------------------------------------------------
# Shared primitives, used by the worker, the ticker and the reconciler too
# --------------------------------------------------------------------------------------


def aware(moment: datetime) -> datetime:
    """Normalise a datetime read back off a model before comparing it to now.

    Postgres round-trips ``timestamptz`` faithfully. SQLite does not: SQLAlchemy stores
    the value as a string with the offset dropped and hands back a naive datetime, so
    ``lease.expires_at <= utcnow()`` raises ``TypeError`` -- on SQLite only, which means
    in the demo and in CI but not in the deployment somebody tested against. Everything
    in the engine that compares a stored timestamp to the clock goes through here.

    Values are written in UTC by :func:`bailment.models.utcnow`, so stamping UTC back on
    a naive value recovers the original instant rather than guessing at one. Comparisons
    performed *in SQL* need no such help: the bound parameter is rendered with the same
    convention the column was written with.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def seconds_remaining(lease: Lease, *, now: datetime | None = None) -> int | None:
    """How long is left on a lease, in seconds. Negative once it is overdue.

    Reimplements :attr:`bailment.models.Lease.seconds_remaining` only to route the stored
    value through :func:`aware` first; see that function for why.
    """
    if lease.expires_at is None:
        return None
    return int((aware(lease.expires_at) - (now or utcnow())).total_seconds())


def claim_is_stale(lease: Lease, *, ttl: timedelta, now: datetime | None = None) -> bool:
    """Whether another worker may take this lease's claim."""
    if lease.claimed_by is None or lease.claimed_at is None:
        return True
    return (now or utcnow()) - aware(lease.claimed_at) > ttl


def record_event(
    session: AsyncSession,
    lease: Lease,
    *,
    actor: str,
    action: str,
    detail: Mapping[str, Any] | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
) -> AuditEvent:
    """Append one audit row.

    Adds the row through the session rather than through ``lease.events``. The
    relationship is ``selectin``-loaded, so appending to it from a code path that got the
    lease some other way triggers a lazy load, and a lazy load in async SQLAlchemy raises
    from whichever line touched the attribute rather than from the line that caused it.
    """
    event = AuditEvent(
        lease_id=lease.id,
        at=utcnow(),
        actor=actor,
        action=action,
        from_state=from_state,
        to_state=to_state,
        detail=dict(detail or {}),
    )
    session.add(event)
    return event


def transition(
    session: AsyncSession,
    lease: Lease,
    target: LeaseState,
    *,
    actor: str,
    action: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Move a lease to ``target``, refusing anything the state machine forbids.

    :func:`bailment.states.assert_transition` is the authority and this is the only
    function that writes ``Lease.state``. Centralising it means a new caller cannot
    invent a shortcut -- the tempting one being a direct write of ``RELEASED``, which
    would make a failed teardown indistinguishable from a successful one and delete the
    orphan from view.
    """
    source = lease.lease_state
    assert_transition(source, target)
    if source != target:
        lease.previous_state = source.value
        lease.state = target.value
    lease.updated_at = utcnow()
    record_event(
        session,
        lease,
        actor=actor,
        action=action,
        detail=detail,
        from_state=source.value,
        to_state=target.value,
    )


def queue_for_provisioning(lease: Lease) -> None:
    """Make a lease immediately visible to a provisioning worker.

    Clears the claim so no dead worker's fingerprint blocks it, and resets the attempt
    counter so an approval granted after a failed first pass gets a full retry budget.
    """
    lease.claimed_by = None
    lease.claimed_at = None
    lease.attempts = 0
    lease.next_attempt_at = utcnow()


def queue_for_teardown(lease: Lease) -> None:
    """Make a lease visible to a teardown worker.

    The attempt counter is reset here too: the budget that matters for teardown is "how
    many times have we tried to destroy this", and inheriting a count from the
    provisioning phase would send a lease to ORPHANED after one failed destroy.
    """
    lease.claimed_by = None
    lease.claimed_at = None
    lease.attempts = 0
    lease.next_attempt_at = utcnow()


def backoff_seconds(attempts: int, *, base: float = 5.0, cap: float = 600.0) -> float:
    """Exponential backoff with jitter, for ``Lease.next_attempt_at``.

    Jittered so that a fleet of workers that all failed against the same rate-limited
    provider does not come back in lockstep and fail together again. Not
    security-sensitive, so ``random`` rather than ``secrets`` is the right tool.
    """
    delay = min(base * (2.0 ** max(0, attempts - 1)), cap)
    return delay * (0.5 + random.random() / 2)  # noqa: S311


def clamp_ttl(path: GoldenPath, requested_seconds: int | None) -> tuple[int, str | None]:
    """Return the TTL that will actually be granted, and a notice if it was moved.

    Clamping, never rejecting. A rejection tells an agent only that it was wrong and it
    retries with another guess; a clamp plus a sentence naming the ceiling tells it the
    shape of the world once.
    """
    maximum = int(path.lease.max_ttl.delta.total_seconds())
    if requested_seconds is None:
        default = int(path.lease.default_ttl.delta.total_seconds())
        return min(default, maximum), None
    if requested_seconds > maximum:
        return maximum, (
            f"requested TTL of {format_duration(timedelta(seconds=requested_seconds))} "
            f"was clamped to the maximum this golden path allows, {path.lease.max_ttl}. "
            f"Renew before it expires if you need longer; this path allows "
            f"{path.lease.max_renewals} renewal(s)."
        )
    if requested_seconds < MIN_TTL_SECONDS:
        return MIN_TTL_SECONDS, (
            f"requested TTL of {requested_seconds}s was raised to the {MIN_TTL_SECONDS}s "
            f"minimum; anything shorter expires before it can be used."
        )
    return requested_seconds, None


def make_external_name(golden_path_id: str, provider: str, lease_id: str) -> str:
    """Mint the name the resource will carry at the provider.

    Three properties, in the order they matter:

    *Readable.* Somebody staring at a cloud console at 3am has to be able to tell what
    this is and where it came from, so the golden path id is in the name and the prefix
    says which system owns it.

    *Deterministic.* Derived from the lease's own primary key, so recomputing it for a
    row always produces the same answer. A name that depended on a fresh random draw
    could not be recovered if the write that stored it were ever lost.

    *Collision-resistant.* 32 bits of uuid4 per golden path. A duplicate would make two
    leases claim one resource, which the unique index on ``(provider, external_name)``
    is not, so the row is also checked against the database before use.

    The prefix comes from :func:`bailment.providers.base.resource_prefix` and never from
    a constant here: if the minting side and the ``list_managed`` filtering side could
    disagree about it, every resource would look like an orphan at once.
    """
    prefix = resource_prefix()
    suffix = _HEX.sub("", lease_id.lower())[:8].ljust(8, "0")
    budget = _NAME_LIMIT - len(prefix) - len(suffix) - 1
    if budget < 3:
        raise ServiceError(
            f"BAILMENT_RESOURCE_PREFIX {prefix!r} leaves no room for a resource name "
            f"within the {_NAME_LIMIT} character limit; use a shorter prefix"
        )
    slug = _SLUG_UNSAFE.sub("-", golden_path_id.lower()).strip("-._")[:budget].strip("-._")
    name = f"{prefix}{slug or 'path'}-{suffix}"
    # One definition of what a legal managed name is, and it lives with the providers
    # that have to recognise it. Raises ProviderError, which the caller wraps.
    return require_managed_name(provider, name, operation="mint-external-name")


def public_output_names(path: GoldenPath | None) -> frozenset[str]:
    """Output names the golden path declares non-secret.

    ``None`` -- a lease whose golden path has since been removed from the catalog --
    yields the empty set, so an output whose classification cannot be established is
    treated as secret. Failing closed here costs a dashboard field; failing open costs a
    credential.
    """
    if path is None:
        return frozenset()
    return frozenset(output.name for output in path.outputs if not output.secret)


def published_outputs(lease: Lease) -> dict[str, str]:
    """The plaintext outputs recorded on the most recent ``activated`` event.

    Reading them from the audit trail rather than from the sealed binding is what keeps
    :meth:`LeaseService.resolve_binding` the only decryption path in the service. The
    worker decided at activation time which names were publishable; nothing downstream
    re-derives that decision, so there is only one place it can be got wrong.
    """
    latest: dict[str, str] = {}
    newest: datetime | None = None
    for event in lease.events:
        if event.action != ACTION_ACTIVATED:
            continue
        at = aware(event.at)
        if newest is not None and at <= newest:
            continue
        raw = event.detail.get("outputs")
        if isinstance(raw, dict):
            latest = {str(k): str(v) for k, v in raw.items()}
            newest = at
    return latest


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class LeaseService:
    """Request and lease operations, over one session.

    Construct per request (or per unit of work in a worker) and throw away. It holds a
    session, so it is neither thread-safe nor safe to share between concurrent tasks --
    which is the same rule an :class:`~sqlalchemy.ext.asyncio.AsyncSession` already has.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        catalog: Catalog,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        policy: PolicyEngine | None = None,
        secret_box: SecretBox | None = None,
        approval_window: timedelta = DEFAULT_APPROVAL_WINDOW,
    ) -> None:
        self.session = session
        self.catalog = catalog
        self.registry = registry
        self.settings = settings or get_settings()
        self.policy = policy or PolicyEngine()
        self.approval_window = approval_window
        self._secret_box = secret_box

    # -- catalog -----------------------------------------------------------------------

    def resolve_path(self, golden_path_id: str) -> GoldenPath:
        """Step 1 of :meth:`request_provision`. Unknown and disabled both raise."""
        try:
            path = self.catalog.get(golden_path_id)
        except UnknownGoldenPath as exc:
            raise UnknownPath(str(exc)) from None
        if not path.enabled:
            raise PathDisabled(
                f"golden path {golden_path_id!r} exists but is disabled and cannot be "
                f"provisioned. Ask a platform operator to enable it, or pick another: "
                f"{', '.join(p.id for p in self.catalog.enabled()) or '<none>'}"
            )
        return path

    def find_path(self, golden_path_id: str) -> GoldenPath | None:
        """Look up without raising. Used when rendering an existing lease whose path may
        since have been removed from the catalog."""
        return self.catalog.find(golden_path_id)

    # -- the entry point ---------------------------------------------------------------

    async def request_provision(
        self, caller: Caller, request: ProvisionRequest
    ) -> ProvisionOutcome:
        """Accept a provisioning request. The single entry point for REST and MCP alike.

        The order below is load-bearing and is the order in the design brief:

        1. resolve the golden path,
        2. validate inputs against its schema, rejecting unknown keys,
        3. honour the idempotency key,
        4. clamp the TTL,
        5. evaluate policy,
        6. mint ``external_name`` and **commit** before anything can reach a provider,
        7. write the audit event.

        Steps 4 and 5 are in that order because policy reads ``ttl_seconds`` and must see
        the granted value. Step 6 is last among the writes because everything before it
        can still refuse the request without having named a resource.
        """
        path = self.resolve_path(request.golden_path_id)

        inputs, requested_ttl = self._split_lease_arguments(request)
        try:
            inputs = validate_inputs(path.inputs, inputs)
        except InputValidationError as exc:
            raise InvalidRequest(
                f"the request does not satisfy the '{path.id}' golden path: "
                f"{'; '.join(exc.problems)}",
                exc.problems,
            ) from None
        except SchemaError as exc:
            raise InvalidRequest(str(exc)) from None

        existing = await self._find_idempotent(caller, request)
        if existing is not None:
            return await self._replay(existing, caller, request, path, inputs)

        ttl_seconds, notice = clamp_ttl(path, requested_ttl)
        notices: list[str] = [notice] if notice else []

        provider_name = path.provider
        if provider_name not in self.registry:
            raise ProviderUnavailable(
                f"golden path {path.id!r} names provider {provider_name!r}, which is not "
                f"registered. Registered providers: {', '.join(self.registry.names())}"
            )

        decision = await self._decide(caller, path, inputs, ttl_seconds)

        lease = Lease(
            golden_path_id=path.id,
            provider=provider_name,
            inputs=dict(inputs),
            requester=caller.principal,
            on_behalf_of=caller.on_behalf_of,
            agent_session=caller.session,
            idempotency_key=request.idempotency_key or None,
            state=LeaseState.PENDING.value,
            ttl_seconds=ttl_seconds,
            estimated_hourly_usd=path.cost.estimated_hourly_usd,
            policy_effect=decision.effect,
            policy_reason=decision.reason,
            policy_rule_index=decision.rule_index,
        )
        # external_name before the row is even flushed. It is derived from the primary
        # key, which is why the id has to be materialised here rather than left to the
        # column default: a default has not run at this point, so ``lease.id`` would be
        # ``None`` and every external name in the deployment would collide.
        lease.id = new_id()
        lease.external_name = make_external_name(path.id, provider_name, lease.id)

        self.session.add(lease)
        record_event(
            self.session,
            lease,
            actor=caller.principal,
            action=ACTION_REQUESTED,
            to_state=LeaseState.PENDING.value,
            detail={
                "caller_kind": caller.kind.value,
                "golden_path": path.id,
                "provider": provider_name,
                "external_name": lease.external_name,
                "ttl_seconds": ttl_seconds,
                "requested_ttl_seconds": requested_ttl,
                "idempotency_key": request.idempotency_key,
                "policy_effect": decision.effect,
                "policy_rule_index": decision.rule_index,
            },
        )

        self._apply_decision(lease, decision, caller, path)

        try:
            await self.session.commit()
        except IntegrityError:
            # Two identical requests raced on (requester, idempotency_key). The loser
            # rolls back everything it staged -- nothing has reached a provider yet,
            # because nothing can reach a provider before this commit -- and returns the
            # winner's lease. That is what the key promised.
            await self.session.rollback()
            winner = await self._find_idempotent(caller, request)
            if winner is None:
                raise
            return await self._replay(winner, caller, request, path, inputs)

        log.info(
            "lease requested",
            lease_id=lease.id,
            golden_path=path.id,
            provider=provider_name,
            state=lease.state,
            requester=caller.principal,
            caller_kind=caller.kind.value,
            external_name=lease.external_name,
            ttl_seconds=ttl_seconds,
            policy_effect=decision.effect,
        )

        if decision.effect == "deny":
            notices.append(decision.reason)
        elif decision.effect == "require_approval":
            notices.append(
                f"this request needs a human decision: {decision.reason} "
                f"The lease id is {lease.id}; poll it or wait for the approval to land."
            )

        return ProvisionOutcome(
            lease=await self._view(lease, path),
            replayed=False,
            ttl_clamped=notice is not None,
            notices=tuple(notices),
        )

    # -- decisions ---------------------------------------------------------------------

    async def _decide(
        self,
        caller: Caller,
        path: GoldenPath,
        inputs: Mapping[str, Any],
        ttl_seconds: int,
    ) -> PolicyDecision:
        context = RequestContext(
            requester=caller.principal,
            inputs=inputs,
            on_behalf_of=caller.on_behalf_of,
            is_agent=caller.is_agent,
            golden_path=path.id,
            ttl_seconds=ttl_seconds,
            estimated_monthly_cost_usd=path.cost.estimated_monthly_usd,
            estimated_hourly_cost_usd=path.cost.estimated_hourly_usd,
            active_leases_for_requester=await self.count_live_leases(caller.principal),
            at=utcnow(),
        )
        return self.policy.evaluate(path, context)

    def _apply_decision(
        self, lease: Lease, decision: PolicyDecision, caller: Caller, path: GoldenPath
    ) -> None:
        """Turn a policy effect into the lease's next state.

        Anything that is not an explicit ``allow`` or ``require_approval`` ends as a
        rejection. The policy engine only ever returns three effects, so the final branch
        is unreachable today -- and it stays, because the day a fourth effect is added
        the safe behaviour is to refuse rather than to fall through to provisioning.
        """
        if decision.effect == "allow":
            queue_for_provisioning(lease)
            record_event(
                self.session,
                lease,
                actor="bailment",
                action=ACTION_QUEUED,
                from_state=lease.state,
                to_state=lease.state,
                detail={"reason": decision.reason},
            )
            return

        if decision.effect == "require_approval":
            approval = Approval(
                lease_id=lease.id,
                reason=decision.reason,
                allowed_approvers=list(decision.approvers),
                requested_at=utcnow(),
                deadline_at=utcnow() + self.approval_window,
            )
            self.session.add(approval)
            transition(
                self.session,
                lease,
                LeaseState.AWAITING_APPROVAL,
                actor="bailment",
                action=ACTION_APPROVAL_REQUESTED,
                detail={
                    "reason": decision.reason,
                    "allowed_approvers": list(decision.approvers),
                    "deadline_at": approval.deadline_at.isoformat()
                    if approval.deadline_at
                    else None,
                    "url": self._approval_url(lease.id),
                    "requested_by": caller.principal,
                    "golden_path": path.id,
                },
            )
            return

        transition(
            self.session,
            lease,
            LeaseState.REJECTED,
            actor="bailment",
            action=ACTION_POLICY_DENIED,
            detail={
                "reason": decision.reason,
                "rule_index": decision.rule_index,
                "error": decision.error,
            },
        )
        lease.failure_reason = decision.reason
        lease.next_attempt_at = None

    # -- idempotency -------------------------------------------------------------------

    async def _find_idempotent(self, caller: Caller, request: ProvisionRequest) -> Lease | None:
        if not request.idempotency_key:
            return None
        result = await self.session.execute(
            select(Lease).where(
                Lease.requester == caller.principal,
                Lease.idempotency_key == request.idempotency_key,
            )
        )
        return result.scalars().first()

    async def _replay(
        self,
        lease: Lease,
        caller: Caller,
        request: ProvisionRequest,
        path: GoldenPath,
        inputs: Mapping[str, Any],
    ) -> ProvisionOutcome:
        """Return an existing lease for a repeated idempotency key, unchanged.

        Unchanged is the promise, so a replay whose parameters differ from the original
        still returns the original. It does not pass silently, though: the mismatch is
        recorded and named in a notice, because a caller reusing one key for two
        different requests has a bug that would otherwise surface as "my second database
        has the wrong name".
        """
        notices = [
            f"idempotency key {request.idempotency_key!r} already produced lease "
            f"{lease.id}; returning it unchanged rather than provisioning again."
        ]
        differs = lease.golden_path_id != request.golden_path_id or lease.inputs != dict(inputs)
        if differs:
            notices.append(
                "the parameters of this call differ from the ones the key was first used "
                "with. The original lease is returned; nothing was changed. Use a fresh "
                "idempotency key for a different request."
            )
            record_event(
                self.session,
                lease,
                actor=caller.principal,
                action=ACTION_REPLAYED,
                from_state=lease.state,
                to_state=lease.state,
                detail={
                    "idempotency_key": request.idempotency_key,
                    "conflict": True,
                    "requested_golden_path": request.golden_path_id,
                },
            )
            await self.session.commit()
            log.warning(
                "idempotency key reused with different parameters",
                lease_id=lease.id,
                idempotency_key=request.idempotency_key,
                requester=caller.principal,
            )

        view_path = self.find_path(lease.golden_path_id) or path
        return ProvisionOutcome(
            lease=await self._view(lease, view_path), replayed=True, notices=tuple(notices)
        )

    # -- approvals ---------------------------------------------------------------------

    async def approve(self, lease_id: str, caller: Caller, *, note: str | None = None) -> LeaseView:
        """Grant an approval and queue the lease for provisioning.

        Agents cannot call this under any circumstance. An approval gate that the thing
        being gated can open is not a gate, and the check is on caller *kind* rather than
        on a permission string so that no token configuration can produce an approving
        agent by accident.
        """
        if caller.is_agent:
            raise NotAuthorized(
                "approvals are a human decision. An agent cannot approve a request, "
                "including its own."
            )
        lease = await self._load(lease_id, caller)
        approval = await self._load_approval(lease)

        if lease.lease_state is not LeaseState.AWAITING_APPROVAL:
            raise ConflictingState(
                f"lease {lease_id} is {lease.state}, not awaiting approval; there is "
                f"nothing to approve"
            )
        if not approval.is_pending:
            raise ConflictingState(
                f"lease {lease_id} was already decided at "
                f"{approval.decided_at.isoformat() if approval.decided_at else 'unknown'} "
                f"by {approval.decided_by}"
            )
        self._check_approver(approval, caller)
        now = utcnow()
        if approval.deadline_at is not None and aware(approval.deadline_at) <= now:
            raise ConflictingState(
                f"the approval window for lease {lease_id} closed at "
                f"{approval.deadline_at.isoformat()}. Ask the requester to submit it "
                f"again; approving something whose requester has moved on is how a "
                f"resource ends up with no owner."
            )

        approval.decided_at = now
        approval.decided_by = caller.principal
        approval.approved = True
        approval.decision_note = note

        transition(
            self.session,
            lease,
            LeaseState.PROVISIONING,
            actor=caller.principal,
            action=ACTION_APPROVED,
            detail={
                "note": note,
                "self_approved": caller.principal == lease.requester,
                "caller_kind": caller.kind.value,
            },
        )
        queue_for_provisioning(lease)
        await self.session.commit()
        log.info(
            "lease approved",
            lease_id=lease.id,
            approver=caller.principal,
            self_approved=caller.principal == lease.requester,
        )
        return await self._view(lease, self.find_path(lease.golden_path_id))

    async def reject(self, lease_id: str, caller: Caller, *, reason: str) -> LeaseView:
        """Decline an approval. Terminal; nothing was provisioned."""
        if caller.is_agent:
            raise NotAuthorized("an agent cannot decide an approval request")
        if not reason.strip():
            raise InvalidRequest(
                "a rejection needs a reason. It is shown verbatim to the requester, who "
                "is usually an agent that will otherwise simply try again."
            )
        lease = await self._load(lease_id, caller)
        approval = await self._load_approval(lease)
        if lease.lease_state is not LeaseState.AWAITING_APPROVAL:
            raise ConflictingState(f"lease {lease_id} is {lease.state}, not awaiting approval")
        if not approval.is_pending:
            raise ConflictingState(f"lease {lease_id} was already decided")
        self._check_approver(approval, caller)

        approval.decided_at = utcnow()
        approval.decided_by = caller.principal
        approval.approved = False
        approval.decision_note = reason

        lease.failure_reason = reason
        lease.next_attempt_at = None
        transition(
            self.session,
            lease,
            LeaseState.REJECTED,
            actor=caller.principal,
            action=ACTION_REJECTED,
            detail={"reason": reason, "caller_kind": caller.kind.value},
        )
        await self.session.commit()
        log.info("lease rejected", lease_id=lease.id, approver=caller.principal)
        return await self._view(lease, self.find_path(lease.golden_path_id))

    def _check_approver(self, approval: Approval, caller: Caller) -> None:
        allowed = list(approval.allowed_approvers or [])
        if allowed and caller.principal not in allowed:
            raise NotAuthorized(
                f"{caller.principal!r} is not on the approver list for this request "
                f"({', '.join(allowed)})"
            )
        if not allowed and not caller.is_operator:
            raise NotAuthorized(
                "this request names no specific approvers, so it needs an operator token to decide"
            )

    # -- lifecycle ---------------------------------------------------------------------

    async def renew(
        self, lease_id: str, caller: Caller, *, ttl: str | None = None
    ) -> ProvisionOutcome:
        """Extend a live lease. Delegates to :mod:`bailment.engine.leases`.

        The rules live there because the ticker and this method must agree about what a
        renewal does to ``expires_at``; two implementations of that is two answers.
        """
        from bailment.engine.leases import RenewalError, renew_lease

        lease = await self._load(lease_id, caller)
        path = self.find_path(lease.golden_path_id)
        if path is None:
            raise ConflictingState(
                f"golden path {lease.golden_path_id!r} is no longer in the catalog, so "
                f"there is no ceiling to renew against. Let the lease expire."
            )
        requested = _parse_ttl(ttl) if ttl else None
        try:
            granted, notice = renew_lease(
                self.session, lease, path, requested_seconds=requested, actor=caller.principal
            )
        except RenewalError as exc:
            raise RenewalRefused(str(exc)) from None
        await self.session.commit()
        log.info(
            "lease renewed",
            lease_id=lease.id,
            requester=caller.principal,
            ttl_seconds=granted,
            renewals=lease.renewals,
        )
        return ProvisionOutcome(
            lease=await self._view(lease, path),
            ttl_clamped=notice is not None,
            notices=(notice,) if notice else (),
        )

    async def revoke(self, lease_id: str, caller: Caller, *, reason: str) -> LeaseView:
        """End a lease early and queue its resource for destruction.

        A request that has not been provisioned yet is *rejected* rather than revoked:
        REVOKED means "there is a resource and it must die", and using it for a lease
        that never had one would put a row in the teardown queue with nothing to tear
        down.
        """
        from bailment.engine.leases import revoke_lease

        if not reason.strip():
            raise InvalidRequest("a revocation needs a reason; it goes in the audit trail")
        lease = await self._load(lease_id, caller)
        if lease.requester != caller.principal and not caller.may_see_everything:
            raise NotAuthorized(
                f"lease {lease_id} belongs to {lease.requester!r}; revoking someone "
                f"else's lease needs an operator token"
            )
        if lease.lease_state in TERMINAL_STATES:
            raise ConflictingState(
                f"lease {lease_id} is already {lease.state} and cannot be revoked"
            )
        try:
            revoke_lease(self.session, lease, actor=caller.principal, reason=reason)
        except IllegalTransition as exc:
            raise ConflictingState(str(exc)) from None
        await self.session.commit()
        log.info("lease revoked", lease_id=lease.id, actor=caller.principal, state=lease.state)
        return await self._view(lease, self.find_path(lease.golden_path_id))

    async def retry_teardown(self, lease_id: str, caller: Caller) -> LeaseView:
        """Put an ORPHANED lease back in the teardown queue.

        Orphans are deliberately not retried automatically: they got there by exhausting
        their attempt budget, and a loop that keeps calling a provider which keeps
        refusing is a loop that hides the problem instead of surfacing it. Somebody
        looking at the dashboard decides to try again, and this is how they do it.
        """
        if not caller.may_see_everything:
            raise NotAuthorized("retrying a teardown needs an operator token")
        lease = await self._load(lease_id, caller)
        if lease.lease_state is not LeaseState.ORPHANED:
            raise ConflictingState(
                f"lease {lease_id} is {lease.state}; only an orphaned lease needs its "
                f"teardown re-driven"
            )
        queue_for_teardown(lease)
        record_event(
            self.session,
            lease,
            actor=caller.principal,
            action=ACTION_QUEUED,
            from_state=lease.state,
            to_state=lease.state,
            detail={"reason": "operator re-queued an orphaned lease for teardown"},
        )
        await self.session.commit()
        return await self._view(lease, self.find_path(lease.golden_path_id))

    # -- reads -------------------------------------------------------------------------

    async def get_lease(self, lease_id: str, caller: Caller) -> LeaseView:
        lease = await self._load(lease_id, caller)
        return await self._view(lease, self.find_path(lease.golden_path_id))

    async def list_leases(
        self,
        caller: Caller,
        *,
        states: Sequence[LeaseState] | None = None,
        golden_path_id: str | None = None,
        provider: str | None = None,
        requester: str | None = None,
        live_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LeaseView]:
        """List leases this caller may see, newest first.

        A caller who is not an operator sees only their own leases and the ones an agent
        raised on their behalf, whatever they pass in ``requester``. The filter is
        applied here rather than trusted from the caller because "show me everything"
        is the most natural thing in the world for an agent to try.
        """
        query = select(Lease).order_by(Lease.created_at.desc())
        if not caller.may_see_everything:
            query = query.where(
                (Lease.requester == caller.principal) | (Lease.on_behalf_of == caller.principal)
            )
        elif requester:
            query = query.where(Lease.requester == requester)

        if states:
            query = query.where(Lease.state.in_([s.value for s in states]))
        if live_only:
            query = query.where(Lease.state.in_([s.value for s in LIVE_STATES]))
        if golden_path_id:
            query = query.where(Lease.golden_path_id == golden_path_id)
        if provider:
            query = query.where(Lease.provider == provider)

        query = query.limit(max(1, min(limit, 500))).offset(max(0, offset))
        result = await self.session.execute(query)
        return [
            await self._view(lease, self.find_path(lease.golden_path_id))
            for lease in result.scalars().all()
        ]

    async def count_live_leases(self, requester: str) -> int:
        """How many leases this principal holds that are believed to cost money.

        Feeds the ``active_leases_for_requester`` policy name, which is the blunt
        instrument that stops a looping agent from provisioning forty databases.
        """
        result = await self.session.execute(
            select(func.count())
            .select_from(Lease)
            .where(
                Lease.requester == requester,
                Lease.state.in_([s.value for s in LIVE_STATES]),
            )
        )
        return int(result.scalar_one())

    # -- the security boundary ---------------------------------------------------------

    async def resolve_binding(self, reference: str, caller: Caller) -> dict[str, str]:
        """Decrypt and return a binding's values. **The only such path in bailment.**

        Callable by the CLI injection path and by a human operator at the dashboard.
        Nothing else -- explicitly not an MCP tool handler, whose caller kind is
        ``AGENT``, and explicitly not the workers, whose kind is ``SYSTEM``. The check is
        on :class:`CallerKind` and not on a scope string or a boolean flag, because a
        caller kind has to be constructed at the surface where the request arrived, and
        an MCP handler cannot produce ``CLI`` without somebody writing the word.

        Every call is recorded and counted. A binding whose ``access_count`` climbs while
        its lease is idle is worth looking at.
        """
        if not caller.may_read_secrets:
            raise SecretAccessDenied(
                f"a {caller.kind.value} caller cannot read binding values. bailment hands "
                f"out capabilities, not credentials: use the reference with 'bailment run "
                f"-- <command>' and the value is injected into that process's environment "
                f"without passing through the caller."
            )
        try:
            binding_id = parse_reference(reference)
        except ValueError as exc:
            raise BindingNotFound(str(exc)) from None

        binding = await self.session.get(Binding, binding_id)
        if binding is None:
            raise BindingNotFound(f"no binding for reference {reference}")

        lease = await self.session.get(Lease, binding.lease_id)
        if lease is None:  # pragma: no cover - cascade makes this unreachable
            raise BindingNotFound(f"binding {reference} has no lease")

        if not caller.may_see_everything and caller.principal not in (
            lease.requester,
            lease.on_behalf_of,
        ):
            raise NotAuthorized(f"binding {reference} does not belong to {caller.principal!r}")

        if binding.is_revoked:
            raise ConflictingState(
                f"binding {reference} was revoked at "
                f"{binding.revoked_at.isoformat() if binding.revoked_at else 'unknown'}; "
                f"the resource behind it no longer exists"
            )
        if lease.lease_state not in USABLE_STATES:
            raise ConflictingState(
                f"lease {lease.id} is {lease.state}. A binding is only usable while its "
                f"lease is active or expiring, because the credential stops working the "
                f"moment the resource is destroyed."
            )

        try:
            values = self.secret_box.open(binding.ciphertext)
        except DecryptionError as exc:
            # The message from secrets.py explains key rotation and contains no material.
            raise ConflictingState(str(exc)) from None

        binding.access_count += 1
        binding.last_accessed_at = utcnow()
        record_event(
            self.session,
            lease,
            actor=caller.principal,
            action=ACTION_BINDING_RESOLVED,
            from_state=lease.state,
            to_state=lease.state,
            detail={
                "reference": reference,
                "caller_kind": caller.kind.value,
                "output_names": sorted(values),
                "access_count": binding.access_count,
            },
        )
        await self.session.commit()
        log.info(
            "binding resolved",
            lease_id=lease.id,
            reference=reference,
            caller_kind=caller.kind.value,
            output_names=sorted(values),
        )
        return values

    @property
    def secret_box(self) -> SecretBox:
        """Built on first use, so a process that never touches a binding never needs a key."""
        if self._secret_box is None:
            self._secret_box = SecretBox.from_settings(self.settings)
        return self._secret_box

    # -- internals ---------------------------------------------------------------------

    def _split_lease_arguments(
        self, request: ProvisionRequest
    ) -> tuple[dict[str, Any], int | None]:
        """Peel ``ttl`` out of the inputs before they meet the path's closed schema.

        :meth:`GoldenPath.input_schema_with_lease` advertises ``ttl`` to agents as though
        it were one of the path's inputs, because an MCP tool has exactly one argument
        object. The path's own schema does not declare it and is closed, so leaving it in
        would make every agent request fail on an unknown key.
        """
        inputs = dict(request.inputs)
        embedded = inputs.pop("ttl", None)
        if embedded is not None and not isinstance(embedded, str):
            raise InvalidRequest(f"ttl must be a duration string such as '2h', got {embedded!r}")
        if request.ttl and embedded and request.ttl != embedded:
            raise InvalidRequest(
                f"the request carries two different TTLs ({request.ttl!r} and "
                f"{embedded!r}); send one"
            )
        chosen = request.ttl or embedded
        return inputs, _parse_ttl(chosen) if chosen else None

    async def _load(self, lease_id: str, caller: Caller) -> Lease:
        lease = await self.session.get(Lease, lease_id)
        if lease is None:
            raise LeaseNotFound(f"no lease with id {lease_id!r}")
        if not caller.may_see_everything and caller.principal not in (
            lease.requester,
            lease.on_behalf_of,
        ):
            # Deliberately the same error as "does not exist". Confirming that a lease id
            # is real to somebody who cannot read it is an enumeration oracle.
            raise LeaseNotFound(f"no lease with id {lease_id!r}")
        return lease

    async def _load_approval(self, lease: Lease) -> Approval:
        result = await self.session.execute(select(Approval).where(Approval.lease_id == lease.id))
        approval = result.scalars().first()
        if approval is None:
            raise ConflictingState(f"lease {lease.id} has no approval request attached")
        return approval

    def _approval_url(self, lease_id: str) -> str:
        base = self.settings.public_base_url.rstrip("/")
        return f"{base}/approvals/{lease_id}"

    async def _view(self, lease: Lease, path: GoldenPath | None) -> LeaseView:
        # A lease that arrived from a query already has all three relationships in hand:
        # they are declared ``lazy="selectin"``, so one extra statement loaded them in
        # bulk. A lease this service just *created* has none of them, and touching one
        # would emit a lazy load -- which in async SQLAlchemy raises ``MissingGreenlet``
        # from whichever attribute access happened to trip it rather than from the code
        # that caused it. Asking what is missing and refreshing exactly that keeps the
        # common path free and the create path correct.
        missing = [name for name in _VIEW_RELATIONSHIPS if name in inspect(lease).unloaded]
        if missing:
            await self.session.refresh(lease, missing)

        public = public_output_names(path)
        recorded = published_outputs(lease)
        # Filter again on read. The worker already filtered on write, and doing it twice
        # means a golden path that reclassifies an output as secret takes effect for
        # leases that were activated before the change.
        outputs = {name: value for name, value in recorded.items() if name in public}

        binding = next((b for b in lease.bindings if not b.is_revoked), None)
        secret_names: tuple[str, ...] = ()
        if binding is not None:
            secret_names = tuple(n for n in sorted(binding.output_names) if n not in public)

        approval_view: ApprovalView | None = None
        approval = lease.approval
        if approval is not None:
            approval_view = ApprovalView(
                id=approval.id,
                reason=approval.reason,
                allowed_approvers=tuple(approval.allowed_approvers or ()),
                requested_at=aware(approval.requested_at),
                deadline_at=aware(approval.deadline_at) if approval.deadline_at else None,
                decided_at=aware(approval.decided_at) if approval.decided_at else None,
                decided_by=approval.decided_by,
                approved=approval.approved,
                decision_note=approval.decision_note,
                url=self._approval_url(lease.id) if approval.is_pending else None,
            )

        lease_policy = path.lease if path is not None else None
        return LeaseView(
            id=lease.id,
            golden_path_id=lease.golden_path_id,
            provider=lease.provider,
            state=lease.lease_state,
            requester=lease.requester,
            on_behalf_of=lease.on_behalf_of,
            inputs=dict(lease.inputs or {}),
            created_at=aware(lease.created_at),
            activated_at=aware(lease.activated_at) if lease.activated_at else None,
            expires_at=aware(lease.expires_at) if lease.expires_at else None,
            released_at=aware(lease.released_at) if lease.released_at else None,
            seconds_remaining=seconds_remaining(lease),
            ttl_seconds=lease.ttl_seconds,
            max_ttl_seconds=(
                int(lease_policy.max_ttl.delta.total_seconds())
                if lease_policy is not None
                else lease.ttl_seconds
            ),
            renewals=lease.renewals,
            max_renewals=lease_policy.max_renewals if lease_policy is not None else 0,
            renewable=lease_policy.renewable if lease_policy is not None else False,
            policy_effect=lease.policy_effect,
            policy_reason=lease.policy_reason,
            external_name=lease.external_name,
            estimated_hourly_usd=lease.estimated_hourly_usd,
            binding_reference=make_reference(binding.id) if binding is not None else None,
            outputs=outputs,
            secret_output_names=secret_names,
            approval=approval_view,
            failure_reason=lease.failure_reason,
        )


def _parse_ttl(raw: str) -> int:
    try:
        return int(parse_duration(raw).total_seconds())
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None


def is_teardown_intent(state: LeaseState) -> bool:
    """Whether the lease is waiting for a worker to destroy its resource."""
    return state in TEARDOWN_INTENT_STATES
