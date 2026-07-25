"""The lease lifecycle state machine.

This module is the single authority on which state transitions are legal. Everything
else in bailment -- the API, the workers, the reconciler, the dashboard -- reads the
transition table from here rather than encoding its own idea of the lifecycle.

The design rule that matters: a resource is only ever considered gone once the
*provider* has confirmed it is gone. A row moving to ``RELEASED`` is a claim about
reality, so nothing may write that state except the deprovision path and the
reconciler. Everything else that wants a lease to end asks for ``EXPIRED`` or
``REVOKED``, which are *intentions*, and lets the engine do the work.

That separation is what makes orphan detection possible at all: if code could write
``RELEASED`` optimistically, a failed destroy would look identical to a successful one
and the orphan would be invisible forever.
"""

from __future__ import annotations

from enum import StrEnum


class LeaseState(StrEnum):
    """Every state a request/lease can occupy.

    Ordered roughly by lifecycle position, not alphabetically, because reading this
    enum top to bottom should tell you the story.
    """

    # --- Intake -------------------------------------------------------------
    PENDING = "pending"
    """Request accepted and persisted. No policy decision made yet."""

    AWAITING_APPROVAL = "awaiting_approval"
    """Policy said a human must decide. Nothing has been provisioned."""

    REJECTED = "rejected"
    """Policy denied it outright, or an approver declined. Terminal."""

    # --- Provisioning -------------------------------------------------------
    PROVISIONING = "provisioning"
    """A worker holds the claim and is calling the provider. Crash-prone window."""

    ACTIVE = "active"
    """Provider confirmed the resource exists. The lease clock is running."""

    EXPIRING = "expiring"
    """Inside the warning window. Still fully usable; a notice has been emitted."""

    # --- Teardown -----------------------------------------------------------
    EXPIRED = "expired"
    """TTL elapsed. An *intention* to destroy -- the resource may still exist."""

    REVOKED = "revoked"
    """A human or policy ended it early. Also an intention, not a fact."""

    DEPROVISIONING = "deprovisioning"
    """A worker holds the claim and is calling the provider's delete path."""

    RELEASED = "released"
    """Provider confirmed the resource is gone. The only clean terminal state."""

    # --- Failure modes ------------------------------------------------------
    FAILED = "failed"
    """Provisioning failed and any partial resource was rolled back. Terminal."""

    ORPHANED = "orphaned"
    """We believe a real resource exists that no live lease accounts for.

    Reached either when a destroy fails repeatedly, or when the reconciler finds a
    resource at the provider with no matching active lease. This is the state the
    whole project exists to make visible.
    """

    UNKNOWN = "unknown"
    """Provider state could not be determined. Never assume; always re-check."""


#: States where a real resource is believed to exist and cost money.
LIVE_STATES: frozenset[LeaseState] = frozenset(
    {
        LeaseState.PROVISIONING,
        LeaseState.ACTIVE,
        LeaseState.EXPIRING,
        LeaseState.EXPIRED,
        LeaseState.REVOKED,
        LeaseState.DEPROVISIONING,
        LeaseState.ORPHANED,
        LeaseState.UNKNOWN,
    }
)

#: States a consumer may actually use the binding from.
USABLE_STATES: frozenset[LeaseState] = frozenset({LeaseState.ACTIVE, LeaseState.EXPIRING})

#: No further transition is possible. The reconciler skips these.
TERMINAL_STATES: frozenset[LeaseState] = frozenset(
    {LeaseState.REJECTED, LeaseState.RELEASED, LeaseState.FAILED}
)

#: States that mean "someone decided this should end" but teardown has not finished.
#: The lease engine picks these up and drives them to DEPROVISIONING.
TEARDOWN_INTENT_STATES: frozenset[LeaseState] = frozenset({LeaseState.EXPIRED, LeaseState.REVOKED})

#: The complete legal transition table. Anything absent here is a bug, not a feature.
TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.PENDING: frozenset(
        {
            LeaseState.AWAITING_APPROVAL,
            LeaseState.PROVISIONING,
            LeaseState.REJECTED,
            LeaseState.FAILED,
        }
    ),
    LeaseState.AWAITING_APPROVAL: frozenset(
        {
            LeaseState.PROVISIONING,
            LeaseState.REJECTED,
            # An approval request can outlive its own deadline.
            LeaseState.EXPIRED,
        }
    ),
    LeaseState.PROVISIONING: frozenset(
        {
            LeaseState.ACTIVE,
            LeaseState.FAILED,
            # Provider call succeeded but we could not read back the result.
            LeaseState.UNKNOWN,
            # Revoked mid-flight; teardown must still run.
            LeaseState.REVOKED,
        }
    ),
    LeaseState.ACTIVE: frozenset(
        {
            LeaseState.EXPIRING,
            LeaseState.EXPIRED,
            LeaseState.REVOKED,
            LeaseState.UNKNOWN,
        }
    ),
    LeaseState.EXPIRING: frozenset(
        {
            LeaseState.EXPIRED,
            LeaseState.REVOKED,
            # Extension granted before the deadline.
            LeaseState.ACTIVE,
            LeaseState.UNKNOWN,
        }
    ),
    LeaseState.EXPIRED: frozenset({LeaseState.DEPROVISIONING, LeaseState.UNKNOWN}),
    LeaseState.REVOKED: frozenset({LeaseState.DEPROVISIONING, LeaseState.UNKNOWN}),
    LeaseState.DEPROVISIONING: frozenset(
        {
            LeaseState.RELEASED,
            # Destroy failed. Not terminal -- it is now our problem to surface.
            LeaseState.ORPHANED,
            LeaseState.UNKNOWN,
        }
    ),
    LeaseState.ORPHANED: frozenset(
        {
            # Retry the destroy.
            LeaseState.DEPROVISIONING,
            # Reconciler confirmed it really is gone after all.
            LeaseState.RELEASED,
            LeaseState.UNKNOWN,
        }
    ),
    LeaseState.UNKNOWN: frozenset(
        {
            LeaseState.ACTIVE,
            LeaseState.ORPHANED,
            LeaseState.RELEASED,
            LeaseState.DEPROVISIONING,
            LeaseState.FAILED,
        }
    ),
    LeaseState.REJECTED: frozenset(),
    LeaseState.RELEASED: frozenset(),
    LeaseState.FAILED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when code attempts a transition the lifecycle does not permit."""

    def __init__(self, source: LeaseState, target: LeaseState) -> None:
        self.source = source
        self.target = target
        super().__init__(
            f"illegal lease transition {source.value} -> {target.value}; "
            f"legal targets are {sorted(t.value for t in TRANSITIONS[source]) or ['<terminal>']}"
        )


def can_transition(source: LeaseState, target: LeaseState) -> bool:
    """Return whether ``source -> target`` is a legal move."""
    return target in TRANSITIONS[source]


def assert_transition(source: LeaseState, target: LeaseState) -> None:
    """Raise :class:`IllegalTransition` unless ``source -> target`` is legal.

    Self-transitions are permitted and treated as no-ops so that idempotent retries
    of the same worker step do not blow up.
    """
    if source == target:
        return
    if not can_transition(source, target):
        raise IllegalTransition(source, target)


def is_live(state: LeaseState) -> bool:
    """Whether a real, probably billable resource is believed to exist."""
    return state in LIVE_STATES


def is_terminal(state: LeaseState) -> bool:
    """Whether the lease is finished and needs no further work."""
    return state in TERMINAL_STATES
