"""The lease state machine.

Most of this file is generated from :data:`TRANSITIONS` rather than written out by hand,
because a table-driven test that enumerates the table it is testing proves nothing about
the table. What the generated tests check is the *shape* of the machine: that every state
is reachable from the table, that terminal means terminal, and above all that ``RELEASED``
has exactly three predecessors.

That last one is the invariant the whole project rests on. ``RELEASED`` is a claim that a
resource is gone, and it may only be written by code that asked a provider and got a clean
answer. If a future edit gave, say, ``ACTIVE -> RELEASED`` an edge, a failed teardown
would become indistinguishable from a successful one and the reconciler would have nothing
left to find. The test below is what stops that edit landing quietly.
"""

from __future__ import annotations

import pytest

from bailment.states import (
    LIVE_STATES,
    TEARDOWN_INTENT_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    USABLE_STATES,
    IllegalTransition,
    LeaseState,
    assert_transition,
    can_transition,
    is_live,
    is_terminal,
)

ALL_STATES = tuple(LeaseState)

LEGAL_PAIRS = [(source, target) for source, targets in TRANSITIONS.items() for target in targets]

#: Moves that must raise. Each one is a mistake somebody could plausibly make, not a
#: random pair: skipping the provisioning window, optimistically writing RELEASED,
#: resurrecting a terminal lease, or concluding "orphaned" straight from a failed create.
ILLEGAL_PAIRS = [
    (LeaseState.PENDING, LeaseState.ACTIVE),
    (LeaseState.PENDING, LeaseState.RELEASED),
    (LeaseState.PENDING, LeaseState.REVOKED),
    (LeaseState.PENDING, LeaseState.ORPHANED),
    (LeaseState.AWAITING_APPROVAL, LeaseState.ACTIVE),
    (LeaseState.AWAITING_APPROVAL, LeaseState.REVOKED),
    (LeaseState.PROVISIONING, LeaseState.RELEASED),
    (LeaseState.PROVISIONING, LeaseState.ORPHANED),
    (LeaseState.PROVISIONING, LeaseState.PENDING),
    (LeaseState.ACTIVE, LeaseState.RELEASED),
    (LeaseState.ACTIVE, LeaseState.PENDING),
    (LeaseState.ACTIVE, LeaseState.DEPROVISIONING),
    (LeaseState.ACTIVE, LeaseState.FAILED),
    (LeaseState.EXPIRING, LeaseState.RELEASED),
    (LeaseState.EXPIRED, LeaseState.ACTIVE),
    (LeaseState.EXPIRED, LeaseState.RELEASED),
    (LeaseState.REVOKED, LeaseState.ACTIVE),
    (LeaseState.REVOKED, LeaseState.RELEASED),
    (LeaseState.DEPROVISIONING, LeaseState.ACTIVE),
    (LeaseState.DEPROVISIONING, LeaseState.FAILED),
    (LeaseState.ORPHANED, LeaseState.ACTIVE),
    (LeaseState.ORPHANED, LeaseState.FAILED),
    (LeaseState.RELEASED, LeaseState.ACTIVE),
    (LeaseState.RELEASED, LeaseState.DEPROVISIONING),
    (LeaseState.FAILED, LeaseState.PROVISIONING),
    (LeaseState.FAILED, LeaseState.ACTIVE),
    (LeaseState.REJECTED, LeaseState.PROVISIONING),
    (LeaseState.REJECTED, LeaseState.ACTIVE),
    (LeaseState.UNKNOWN, LeaseState.PENDING),
    (LeaseState.UNKNOWN, LeaseState.EXPIRING),
]


@pytest.mark.parametrize(("source", "target"), LEGAL_PAIRS)
def test_every_tabled_transition_is_permitted(source: LeaseState, target: LeaseState) -> None:
    assert can_transition(source, target)
    assert_transition(source, target)


@pytest.mark.parametrize(("source", "target"), ILLEGAL_PAIRS)
def test_illegal_transitions_raise(source: LeaseState, target: LeaseState) -> None:
    assert not can_transition(source, target)
    with pytest.raises(IllegalTransition) as caught:
        assert_transition(source, target)
    assert caught.value.source is source
    assert caught.value.target is target
    # The message has to name both states and the legal alternatives, because the reader
    # is usually somebody staring at a traceback from a worker at 3am.
    message = str(caught.value)
    assert source.value in message
    assert target.value in message


@pytest.mark.parametrize("state", ALL_STATES)
def test_self_transition_is_a_no_op(state: LeaseState) -> None:
    """Idempotent retries of the same worker step must not blow up.

    Note that this holds for terminal states too: ``assert_transition`` short-circuits on
    equality before it consults the table, which is why ``RELEASED -> RELEASED`` passes
    while ``can_transition`` still reports false for it.
    """
    assert_transition(state, state)
    if state in TERMINAL_STATES:
        assert not can_transition(state, state)


@pytest.mark.parametrize("state", ALL_STATES)
def test_every_state_has_a_transition_entry(state: LeaseState) -> None:
    """A state missing from the table would raise ``KeyError`` on the first move out of it."""
    assert state in TRANSITIONS


@pytest.mark.parametrize(("source", "target"), LEGAL_PAIRS)
def test_transition_targets_are_real_states(source: LeaseState, target: LeaseState) -> None:
    del source
    assert isinstance(target, LeaseState)


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
        assert is_terminal(state)


def test_only_a_confirmed_teardown_can_write_released() -> None:
    """The invariant this project is built on. See the module docstring.

    ``DEPROVISIONING`` is a worker that called destroy. ``ORPHANED`` and ``UNKNOWN`` are
    the reconciler, which asked the provider and was told the resource is gone. Nothing
    else may assert that a resource stopped existing.
    """
    predecessors = {
        source for source, targets in TRANSITIONS.items() if LeaseState.RELEASED in targets
    }
    assert predecessors == {
        LeaseState.DEPROVISIONING,
        LeaseState.ORPHANED,
        LeaseState.UNKNOWN,
    }


def test_set_membership() -> None:
    assert {LeaseState.ACTIVE, LeaseState.EXPIRING} == USABLE_STATES
    assert {LeaseState.REJECTED, LeaseState.RELEASED, LeaseState.FAILED} == TERMINAL_STATES
    assert {LeaseState.EXPIRED, LeaseState.REVOKED} == TEARDOWN_INTENT_STATES
    assert {
        LeaseState.PROVISIONING,
        LeaseState.ACTIVE,
        LeaseState.EXPIRING,
        LeaseState.EXPIRED,
        LeaseState.REVOKED,
        LeaseState.DEPROVISIONING,
        LeaseState.ORPHANED,
        LeaseState.UNKNOWN,
    } == LIVE_STATES


def test_usable_implies_live_and_live_excludes_terminal() -> None:
    """A lease you can use must be one we believe has a resource, and vice versa.

    If ``USABLE`` ever escaped ``LIVE``, an agent could hold a working credential for a
    lease that the cost report and the reconciler both consider finished.
    """
    assert USABLE_STATES <= LIVE_STATES
    assert not (LIVE_STATES & TERMINAL_STATES)
    assert TEARDOWN_INTENT_STATES <= LIVE_STATES


@pytest.mark.parametrize("state", ALL_STATES)
def test_is_live_and_is_terminal_agree_with_the_sets(state: LeaseState) -> None:
    assert is_live(state) == (state in LIVE_STATES)
    assert is_terminal(state) == (state in TERMINAL_STATES)
    # Every state is exactly one of: live, terminal, or not yet started. PENDING,
    # AWAITING_APPROVAL and the three terminals are the only non-live ones.
    assert not (is_live(state) and is_terminal(state))


def test_pending_states_are_not_live() -> None:
    """Nothing costs money before a provider has been called."""
    assert not is_live(LeaseState.PENDING)
    assert not is_live(LeaseState.AWAITING_APPROVAL)


def test_state_values_are_stable_strings() -> None:
    """The values are persisted in a ``String(32)`` column and appear in API responses.

    Renaming one silently invalidates every stored row, so the spellings are pinned here.
    """
    assert [state.value for state in LeaseState] == [
        "pending",
        "awaiting_approval",
        "rejected",
        "provisioning",
        "active",
        "expiring",
        "expired",
        "revoked",
        "deprovisioning",
        "released",
        "failed",
        "orphaned",
        "unknown",
    ]
    assert all(len(state.value) <= 32 for state in LeaseState)
