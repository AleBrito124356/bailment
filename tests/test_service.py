"""The service layer: the one door into the engine.

Four invariants hold only because every surface goes through this module, and each one
has a test here that would fail if a handler started doing the work itself:

* nothing reaches a provider until the row that names it is durable,
* a secret value leaves the system through exactly one function,
* policy is evaluated against the TTL that will actually be granted,
* a repeated idempotency key provisions once and returns the same lease.

The idempotency and deny-path tests run a real worker afterwards and assert on
``provider.calls``. Asserting that the lease table looks right is not enough: the failure
those tests exist to catch is a second resource at the provider, and only the provider can
say whether one was created.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bailment.catalog.schema import BindingOutput, PolicyRule
from bailment.engine.service import (
    MIN_TTL_SECONDS,
    BindingNotFound,
    Caller,
    CallerKind,
    ConflictingState,
    InvalidRequest,
    LeaseNotFound,
    LeaseService,
    NotAuthorized,
    PathDisabled,
    ProviderUnavailable,
    ProvisionRequest,
    SecretAccessDenied,
    UnknownPath,
    backoff_seconds,
    clamp_ttl,
    make_external_name,
)
from bailment.models import Approval, Lease, utcnow
from bailment.providers.base import resource_prefix
from bailment.states import LeaseState
from conftest import (
    PUBLISHED_OUTPUT,
    SEALED_OUTPUT,
    SEALED_VALUE_PREFIX,
    RecordingProvider,
    make_catalog,
    make_path,
)

# --------------------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------------------


async def test_a_permitted_request_is_queued_and_committed(
    request_lease, read_lease, provider: RecordingProvider
) -> None:
    outcome = await request_lease()

    assert outcome.lease.state is LeaseState.PENDING
    assert outcome.replayed is False
    assert outcome.lease.policy_effect == "allow"

    stored = await read_lease(outcome.lease.id)
    assert stored.state == LeaseState.PENDING.value
    assert stored.next_attempt_at is not None
    assert stored.claimed_by is None
    # Requesting is not provisioning: nothing has been called yet.
    assert provider.ops == []


async def test_external_name_is_committed_before_any_provider_could_be_called(
    request_lease, read_lease, provider: RecordingProvider
) -> None:
    """Write-ahead naming, and the whole reason orphan detection is possible.

    If the worker died between the create call and the response, the row would already
    know the name and tag the resource was going to carry. A system that named resources
    from the provider's response has nothing to look for.
    """
    outcome = await request_lease()

    stored = await read_lease(outcome.lease.id)
    assert stored.external_name
    assert stored.external_name.startswith(resource_prefix())
    assert provider.ops == []

    # Deterministic: recomputing it for the row always produces the same answer, which is
    # what makes the name recoverable if the write that stored it were ever lost.
    assert stored.external_name == make_external_name("sandbox", "memory", stored.id)
    assert stored.external_name == outcome.lease.external_name


async def test_the_request_and_the_decision_are_both_recorded(request_lease, read_events) -> None:
    """Two rows: what was asked for, and what the policy chain did about it.

    Compared as a set rather than a list. Both are written in the same instant and the
    audit trail's tiebreak is the row id, which is a uuid -- so their relative order is
    stable per row but arbitrary between runs, and asserting on it would make this test
    flake on a machine with a coarse clock.
    """
    outcome = await request_lease()
    actions = [event.action for event in await read_events(outcome.lease.id)]
    assert sorted(actions) == ["queued", "requested"]


async def test_an_unknown_path_and_a_disabled_one_answer_differently_to_the_service(
    request_lease,
) -> None:
    """Different exceptions here; the API deliberately flattens both to 404."""
    with pytest.raises(UnknownPath):
        await request_lease("nope")
    with pytest.raises(PathDisabled):
        await request_lease("switched-off")


async def test_a_path_whose_provider_is_not_registered_is_refused_up_front(
    request_lease,
) -> None:
    """Better here than as a lease that sits in PENDING with nobody able to say why."""
    with pytest.raises(ProviderUnavailable, match="ghost"):
        await request_lease("nowhere")


async def test_inputs_are_validated_against_the_path_schema(request_lease) -> None:
    with pytest.raises(InvalidRequest) as caught:
        await request_lease("sandbox", inputs={})
    assert caught.value.problems
    assert "name" in caught.value.problems[0]

    with pytest.raises(InvalidRequest) as caught:
        await request_lease("sandbox", inputs={"name": "x", "nonsense": 1})
    assert "not an accepted input" in caught.value.problems[0]


async def test_declared_defaults_are_persisted_on_the_lease(request_lease, read_lease) -> None:
    """A rule reading ``input.simulate`` must see the value the provider will see, and a
    lease row that omits a defaulted field cannot be replayed."""
    outcome = await request_lease("sandbox", inputs={"name": "thing"})
    stored = await read_lease(outcome.lease.id)
    assert stored.inputs == {"name": "thing", "simulate": "ok"}


# --------------------------------------------------------------------------------------
# TTL
# --------------------------------------------------------------------------------------


async def test_a_ttl_above_the_ceiling_is_clamped_and_not_rejected(
    request_lease, read_lease
) -> None:
    """A rejection tells an agent only that it was wrong and it retries with another guess.

    A clamp plus a sentence naming the ceiling tells it the shape of the world once.
    """
    outcome = await request_lease("sandbox", ttl="48h")

    assert outcome.ttl_clamped is True
    assert outcome.lease.ttl_seconds == 3600  # the sandbox path's max_ttl of 1h
    assert outcome.lease.max_ttl_seconds == 3600
    notice = "\n".join(outcome.notices)
    assert "clamped" in notice
    assert "1h" in notice
    assert (await read_lease(outcome.lease.id)).ttl_seconds == 3600


async def test_a_ttl_below_the_floor_is_raised(request_lease) -> None:
    outcome = await request_lease("sandbox", ttl="5s")
    assert outcome.lease.ttl_seconds == MIN_TTL_SECONDS
    assert "minimum" in "\n".join(outcome.notices)


async def test_no_ttl_means_the_path_default(request_lease) -> None:
    assert (await request_lease()).lease.ttl_seconds == 300


async def test_ttl_may_arrive_inside_the_inputs_as_the_tool_schema_advertises(
    request_lease, read_lease
) -> None:
    """An MCP tool has one argument object, so ``ttl`` is advertised as if it were an
    input. The path's own schema is closed and does not declare it, so it has to be peeled
    off before validation or every agent request would fail on an unknown key."""
    outcome = await request_lease("sandbox", inputs={"name": "thing", "ttl": "10m"})
    assert outcome.lease.ttl_seconds == 600
    assert "ttl" not in (await read_lease(outcome.lease.id)).inputs


async def test_two_different_ttls_in_one_request_are_refused(service, agent) -> None:
    with pytest.raises(InvalidRequest, match="two different TTLs"):
        await service.request_provision(
            agent,
            ProvisionRequest(
                golden_path_id="sandbox", inputs={"name": "thing", "ttl": "10m"}, ttl="20m"
            ),
        )


async def test_a_ttl_that_is_not_a_duration_is_refused(request_lease) -> None:
    with pytest.raises(InvalidRequest, match="duration"):
        await request_lease("sandbox", ttl="forever")
    with pytest.raises(InvalidRequest, match="duration string"):
        await request_lease("sandbox", inputs={"name": "thing", "ttl": 600})


def test_clamp_ttl_in_isolation() -> None:
    path = make_path("clamped", default_ttl="4h", max_ttl="8h", warn_before="30m")
    assert clamp_ttl(path, None) == (14400, None)
    assert clamp_ttl(path, 3600)[0] == 3600
    assert clamp_ttl(path, 999_999)[0] == 28800
    assert clamp_ttl(path, 999_999)[1] is not None
    assert clamp_ttl(path, 1)[0] == MIN_TTL_SECONDS


async def test_policy_sees_the_granted_ttl_not_the_requested_one(
    session: AsyncSession, registry, settings, secret_box, agent
) -> None:
    """Clamping happens before the policy context is built, so a rule reading
    ``ttl_seconds`` compares against what will actually happen."""
    path = make_path(
        "capped",
        default_ttl="1h",
        max_ttl="2h",
        warn_before="10m",
        policy=[
            PolicyRule(
                when="ttl_seconds > 7200", effect="deny", reason="unreachable after clamping"
            ),
            PolicyRule(effect="allow", reason="within the ceiling"),
        ],
    )
    service = LeaseService(
        session,
        catalog=make_catalog(path),
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )
    outcome = await service.request_provision(
        agent, ProvisionRequest(golden_path_id="capped", inputs={"name": "x"}, ttl="100h")
    )
    assert outcome.lease.state is LeaseState.PENDING
    assert outcome.lease.ttl_seconds == 7200


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


async def test_a_repeated_idempotency_key_returns_the_same_lease_and_provisions_once(
    request_lease, worker, provider: RecordingProvider, session: AsyncSession
) -> None:
    """The promise, end to end.

    Asserting that both calls returned the same lease id is not sufficient: the failure
    this guards against is a *second resource at the provider*, so the worker runs and the
    provider's own call counter is the assertion.
    """
    first = await request_lease(idempotency_key="task-42")
    second = await request_lease(idempotency_key="task-42")

    assert second.lease.id == first.lease.id
    assert second.replayed is True
    assert "returning it unchanged" in "\n".join(second.notices)

    total = await session.execute(select(func.count()).select_from(Lease))
    assert total.scalar_one() == 1

    await worker.tick()
    assert provider.calls["create"] == 1
    assert len(provider.snapshot()) == 1

    # And a third call after the resource exists still does not make another one.
    third = await request_lease(idempotency_key="task-42")
    assert third.lease.id == first.lease.id
    await worker.tick()
    assert provider.calls["create"] == 1


async def test_a_reused_key_with_different_parameters_returns_the_original_and_says_so(
    request_lease, read_events
) -> None:
    """Unchanged is the promise, so a mismatched replay still returns the original.

    It does not pass silently, though: a caller reusing one key for two different requests
    has a bug that would otherwise surface as "my second database has the wrong name".
    """
    first = await request_lease(idempotency_key="task-42", inputs={"name": "one"})
    second = await request_lease(idempotency_key="task-42", inputs={"name": "two"})

    assert second.lease.id == first.lease.id
    assert second.lease.inputs["name"] == "one"
    assert "differ from the ones the key was first used with" in "\n".join(second.notices)
    assert "idempotent_replay" in [event.action for event in await read_events(first.lease.id)]


async def test_idempotency_keys_are_scoped_to_the_requester(
    request_lease, agent, other_agent
) -> None:
    """A process-wide key would hand one agent's resource to another."""
    mine = await request_lease(caller=agent, idempotency_key="shared")
    theirs = await request_lease(caller=other_agent, idempotency_key="shared")
    assert mine.lease.id != theirs.lease.id


async def test_requests_without_a_key_are_never_collapsed(request_lease) -> None:
    first = await request_lease()
    second = await request_lease()
    assert first.lease.id != second.lease.id


# --------------------------------------------------------------------------------------
# Policy outcomes
# --------------------------------------------------------------------------------------


async def test_a_denied_request_never_reaches_the_provider(
    request_lease, read_lease, worker, provider: RecordingProvider
) -> None:
    """The load-bearing half of a denial. A rejected lease that a worker still picks up
    would make the policy engine decorative."""
    outcome = await request_lease("forbidden", inputs={})

    assert outcome.lease.state is LeaseState.REJECTED
    assert outcome.lease.policy_effect == "deny"
    assert "not handed out" in outcome.lease.policy_reason

    stored = await read_lease(outcome.lease.id)
    assert stored.next_attempt_at is None
    assert stored.failure_reason == outcome.lease.policy_reason

    await worker.tick()
    assert provider.ops == []
    assert provider.calls == {}
    assert (await read_lease(outcome.lease.id)).state == LeaseState.REJECTED.value


async def test_the_denial_reason_is_returned_to_the_caller_verbatim(request_lease) -> None:
    outcome = await request_lease("forbidden", inputs={})
    assert outcome.lease.policy_reason in "\n".join(outcome.notices)


async def test_an_approval_gate_provisions_nothing_until_a_human_answers(
    request_lease, read_lease, worker, provider: RecordingProvider, service, operator
) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})

    assert outcome.lease.state is LeaseState.AWAITING_APPROVAL
    assert outcome.lease.approval is not None
    assert outcome.lease.approval.deadline_at is not None
    assert "needs a human decision" in "\n".join(outcome.notices)

    # A worker must not touch it, however many times it runs.
    await worker.tick()
    await worker.tick()
    assert provider.ops == []
    assert (await read_lease(outcome.lease.id)).state == LeaseState.AWAITING_APPROVAL.value

    view = await service.approve(outcome.lease.id, operator, note="reproducing a bug")
    assert view.state is LeaseState.PROVISIONING

    await worker.tick()
    stored = await read_lease(outcome.lease.id)
    assert stored.state == LeaseState.ACTIVE.value
    assert provider.calls["create"] == 1


async def test_the_lease_clock_starts_at_activation_not_at_request_time(
    request_lease, read_lease, worker, service, operator
) -> None:
    """A lease that spent three hours waiting for an approver has not been holding a
    resource for three hours."""
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    assert (await read_lease(outcome.lease.id)).expires_at is None

    await service.approve(outcome.lease.id, operator)
    await worker.tick()

    stored = await read_lease(outcome.lease.id)
    assert stored.activated_at is not None
    assert stored.expires_at is not None
    assert stored.expires_at - stored.activated_at == timedelta(seconds=stored.ttl_seconds)


async def test_the_same_path_allows_a_request_that_does_not_trip_the_rule(
    request_lease,
) -> None:
    outcome = await request_lease("gated", inputs={"env": "dev", "name": "orders"})
    assert outcome.lease.state is LeaseState.PENDING


# --------------------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------------------


async def test_an_agent_cannot_approve_anything_including_its_own_request(
    request_lease, service, agent
) -> None:
    """The check is on caller *kind* rather than on a permission string, so that no token
    configuration can produce an approving agent by accident."""
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    with pytest.raises(NotAuthorized, match="human decision"):
        await service.approve(outcome.lease.id, agent)
    with pytest.raises(NotAuthorized):
        await service.reject(outcome.lease.id, agent, reason="no")


@pytest.fixture
def named_approver_service(session: AsyncSession, registry, settings, secret_box) -> LeaseService:
    path = make_path(
        "named",
        policy=[
            PolicyRule(
                effect="require_approval",
                reason="alice decides this one",
                approvers=["alice"],
            )
        ],
    )
    return LeaseService(
        session,
        catalog=make_catalog(path),
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )


async def test_a_named_approver_list_is_enforced(
    named_approver_service: LeaseService, agent, operator
) -> None:
    """Holding an operator token is not the same as being on the list."""
    service = named_approver_service
    outcome = await service.request_provision(
        agent, ProvisionRequest(golden_path_id="named", inputs={"name": "x"})
    )
    with pytest.raises(NotAuthorized, match="not on the approver list"):
        await service.approve(outcome.lease.id, operator)
    assert (
        await service.approve(outcome.lease.id, Caller.operator("alice"))
    ).state is LeaseState.PROVISIONING


async def test_a_named_approver_who_is_not_an_operator_cannot_see_the_lease(
    named_approver_service: LeaseService, agent
) -> None:
    """Documenting a real edge in the current design, not asserting that it is ideal.

    :meth:`LeaseService.approve` loads the lease through the ordinary visibility check
    before it consults the approver list, and that check passes only for the requester,
    the human they are acting for, and anyone who may see everything. So a rule that names
    an approver who holds no operator token produces an approval nobody can grant -- the
    caller gets ``LeaseNotFound``, which is also what they would get for an id that does
    not exist.
    """
    outcome = await named_approver_service.request_provision(
        agent, ProvisionRequest(golden_path_id="named", inputs={"name": "x"})
    )
    with pytest.raises(LeaseNotFound):
        await named_approver_service.approve(outcome.lease.id, Caller.human("alice"))


async def test_an_unnamed_approver_list_needs_an_operator_token(
    request_lease, service, human
) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    with pytest.raises(NotAuthorized, match="operator token"):
        await service.approve(outcome.lease.id, human)


async def test_a_decision_cannot_be_made_twice(request_lease, service, operator) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    await service.approve(outcome.lease.id, operator)
    with pytest.raises(ConflictingState, match="not awaiting approval"):
        await service.approve(outcome.lease.id, operator)


async def test_approving_after_the_window_closed_is_refused(
    request_lease, service, operator, session: AsyncSession
) -> None:
    """Approving something whose requester has moved on is how a resource ends up with no
    owner."""
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    approval = (
        (await session.execute(select(Approval).where(Approval.lease_id == outcome.lease.id)))
        .scalars()
        .one()
    )
    approval.deadline_at = utcnow() - timedelta(minutes=1)
    await session.commit()

    with pytest.raises(ConflictingState, match="approval window"):
        await service.approve(outcome.lease.id, operator)


async def test_a_rejection_needs_a_reason_and_ends_the_lease(
    request_lease, service, operator, read_lease
) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    with pytest.raises(InvalidRequest, match="needs a reason"):
        await service.reject(outcome.lease.id, operator, reason="   ")

    view = await service.reject(outcome.lease.id, operator, reason="use staging instead")
    assert view.state is LeaseState.REJECTED
    stored = await read_lease(outcome.lease.id)
    assert stored.failure_reason == "use staging instead"
    assert stored.approval is not None
    assert stored.approval.approved is False


async def test_approving_a_lease_that_has_no_approval_request_is_a_conflict(
    request_lease, service, operator
) -> None:
    outcome = await request_lease()
    with pytest.raises(ConflictingState, match="no approval request"):
        await service.approve(outcome.lease.id, operator)


# --------------------------------------------------------------------------------------
# The secret boundary
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [CallerKind.AGENT, CallerKind.HUMAN, CallerKind.SYSTEM])
async def test_only_the_cli_and_an_operator_may_open_a_binding(
    activate, service, kind: CallerKind
) -> None:
    """An agent that can decrypt its own binding makes the reference indirection
    decorative, and the workers have no reason to read a value at all."""
    lease = await activate()
    reference = f"bailment://binding/{lease.bindings[0].id}"
    caller = Caller("dana", kind)

    assert caller.may_read_secrets is False
    with pytest.raises(SecretAccessDenied) as caught:
        await service.resolve_binding(reference, caller)
    assert "bailment run" in str(caught.value) or "bailment exec" in str(caught.value)


async def test_the_cli_and_an_operator_get_the_values(
    activate, service, cli_caller, operator, sealed_values
) -> None:
    lease = await activate()
    reference = f"bailment://binding/{lease.bindings[0].id}"
    expected = sealed_values(lease)

    assert await service.resolve_binding(reference, cli_caller) == expected
    assert await service.resolve_binding(reference, operator) == expected
    assert expected[SEALED_OUTPUT].startswith(SEALED_VALUE_PREFIX)


async def test_resolving_a_binding_is_counted_and_audited(
    activate, service, cli_caller, read_lease, read_events
) -> None:
    """A binding whose access count climbs while its lease is idle is worth looking at."""
    lease = await activate()
    reference = f"bailment://binding/{lease.bindings[0].id}"

    await service.resolve_binding(reference, cli_caller)
    await service.resolve_binding(reference, cli_caller)

    stored = await read_lease(lease.id)
    assert stored.bindings[0].access_count == 2
    assert stored.bindings[0].last_accessed_at is not None

    resolved = [e for e in await read_events(lease.id) if e.action == "binding_resolved"]
    assert len(resolved) == 2
    # The audit row names the outputs and never their values.
    assert resolved[0].detail["output_names"] == sorted([SEALED_OUTPUT, PUBLISHED_OUTPUT])
    assert SEALED_VALUE_PREFIX not in str(resolved[0].detail)


async def test_a_binding_belonging_to_somebody_else_is_refused(
    activate, service, cli_caller
) -> None:
    lease = await activate()
    reference = f"bailment://binding/{lease.bindings[0].id}"
    with pytest.raises(NotAuthorized, match="does not belong"):
        await service.resolve_binding(reference, Caller.cli("someone-else"))
    # ...but the CLI acting for the human the agent named is fine.
    assert await service.resolve_binding(reference, cli_caller)


@pytest.mark.parametrize(
    "reference",
    ["bailment://binding/nope", "not-a-reference", "bailment://binding/"],
)
async def test_an_unresolvable_reference_is_a_not_found(service, operator, reference) -> None:
    with pytest.raises(BindingNotFound):
        await service.resolve_binding(reference, operator)


async def test_a_binding_is_unusable_once_its_lease_stops_being_usable(
    activate, service, operator, session: AsyncSession
) -> None:
    """The credential stops working the moment the resource is destroyed, so handing it
    out after that is handing out something that no longer opens anything."""
    lease = await activate()
    reference = f"bailment://binding/{lease.bindings[0].id}"

    await service.revoke(lease.id, operator, reason="done")
    with pytest.raises(ConflictingState, match="active or expiring"):
        await service.resolve_binding(reference, operator)


async def test_a_revoked_binding_is_refused_before_it_would_ever_be_opened(
    activate, service, operator, session: AsyncSession
) -> None:
    lease = await activate()
    binding = await session.get(type(lease.bindings[0]), lease.bindings[0].id)
    assert binding is not None
    binding.revoked_at = utcnow()
    await session.commit()

    with pytest.raises(ConflictingState, match="revoked"):
        await service.resolve_binding(f"bailment://binding/{binding.id}", operator)


def test_caller_kinds_carry_their_own_authority() -> None:
    assert Caller.agent("a").is_agent
    assert Caller.operator("o").is_operator
    assert Caller.operator("o").may_read_secrets
    assert Caller.cli("c").may_read_secrets
    assert not Caller.system().may_read_secrets
    assert not Caller.human("h").may_read_secrets
    assert Caller.operator("o").may_see_everything
    assert Caller.system().may_see_everything
    assert not Caller.agent("a").may_see_everything


# --------------------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------------------


async def test_a_view_publishes_non_secret_outputs_and_names_the_rest(
    activate, service, agent, sealed_values
) -> None:
    lease = await activate()
    values = sealed_values(lease)
    view = await service.get_lease(lease.id, agent)

    assert view.outputs == {PUBLISHED_OUTPUT: values[PUBLISHED_OUTPUT]}
    assert view.secret_output_names == (SEALED_OUTPUT,)
    assert view.binding_reference == f"bailment://binding/{lease.bindings[0].id}"
    assert values[SEALED_OUTPUT] not in str(view.as_dict())
    assert view.usable is True


async def test_reclassifying_an_output_as_secret_takes_effect_for_existing_leases(
    activate, session: AsyncSession, registry, settings, secret_box, agent
) -> None:
    """The filter runs on write and again on read, so tightening a golden path applies to
    leases that were activated before the change."""
    lease = await activate()
    # The same path id, now declaring both outputs secret.
    tightened = make_path(
        "sandbox",
        outputs=[
            BindingOutput(name=SEALED_OUTPUT, secret=True),
            BindingOutput(name=PUBLISHED_OUTPUT, secret=True),
        ],
    )
    service = LeaseService(
        session,
        catalog=make_catalog(tightened),
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )
    view = await service.get_lease(lease.id, agent)
    # The value is still sitting in plain text on the ``activated`` audit event, and the
    # read-side filter is what stops it being published now that the path has changed.
    assert view.outputs == {}
    assert set(view.secret_output_names) == {SEALED_OUTPUT, PUBLISHED_OUTPUT}


async def test_a_lease_whose_path_has_left_the_catalog_still_renders(
    activate, session: AsyncSession, registry, settings, secret_box, agent
) -> None:
    """Failing closed costs a dashboard field; failing open costs a credential."""
    lease = await activate()
    service = LeaseService(
        session,
        catalog=make_catalog(make_path("something-else")),
        registry=registry,
        settings=settings,
        secret_box=secret_box,
    )
    view = await service.get_lease(lease.id, agent)
    assert view.outputs == {}
    assert view.renewable is False
    assert view.max_renewals == 0


# --------------------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------------------


async def test_a_caller_cannot_read_a_lease_that_is_not_theirs(
    request_lease, service, other_agent
) -> None:
    """404, not 403, and deliberately the same error as "does not exist": confirming that
    a lease id is real to somebody who cannot read it is an enumeration oracle."""
    outcome = await request_lease()
    with pytest.raises(LeaseNotFound):
        await service.get_lease(outcome.lease.id, other_agent)


async def test_an_operator_sees_everything(request_lease, service, operator) -> None:
    outcome = await request_lease()
    assert (await service.get_lease(outcome.lease.id, operator)).id == outcome.lease.id


async def test_the_human_an_agent_acts_for_can_read_the_lease(
    request_lease, service, human
) -> None:
    outcome = await request_lease()
    assert (await service.get_lease(outcome.lease.id, human)).on_behalf_of == "dana"


async def test_listing_is_scoped_by_the_service_and_not_by_the_caller(
    request_lease, service, agent, other_agent, operator
) -> None:
    """ "Show me everything" is the most natural thing in the world for an agent to try."""
    mine = await request_lease(caller=agent)
    theirs = await request_lease(caller=other_agent)

    # The requester filter is ignored for anyone who may not see everything.
    seen = await service.list_leases(agent, requester="agent-two")
    assert [view.id for view in seen] == [mine.lease.id]

    assert {view.id for view in await service.list_leases(operator)} == {
        mine.lease.id,
        theirs.lease.id,
    }
    assert [view.id for view in await service.list_leases(operator, requester="agent-two")] == [
        theirs.lease.id
    ]


async def test_listing_filters_compose(request_lease, service, operator) -> None:
    await request_lease("sandbox")
    gated = await request_lease("gated", inputs={"env": "dev", "name": "x"})

    by_path = await service.list_leases(operator, golden_path_id="gated")
    assert [view.id for view in by_path] == [gated.lease.id]
    assert len(await service.list_leases(operator, provider="memory")) == 2
    assert await service.list_leases(operator, provider="nobody") == []
    assert len(await service.list_leases(operator, states=[LeaseState.PENDING])) == 2
    assert await service.list_leases(operator, states=[LeaseState.ACTIVE]) == []


async def test_live_leases_are_counted_for_the_quota_rule(activate, service, request_lease) -> None:
    """The blunt instrument that stops a looping agent provisioning forty databases."""
    assert await service.count_live_leases("agent-one") == 0
    await activate()
    assert await service.count_live_leases("agent-one") == 1
    # A pending request is not live: nothing has been created for it.
    await request_lease()
    assert await service.count_live_leases("agent-one") == 1
    await service.list_leases(Caller.operator("o"), live_only=True)


# --------------------------------------------------------------------------------------
# Lifecycle operations
# --------------------------------------------------------------------------------------


async def test_revoking_a_live_lease_queues_it_for_destruction(
    activate, service, agent, read_lease
) -> None:
    lease = await activate()
    view = await service.revoke(lease.id, agent, reason="finished")

    assert view.state is LeaseState.REVOKED
    stored = await read_lease(lease.id)
    assert stored.next_attempt_at is not None
    assert stored.claimed_by is None


async def test_revoking_a_request_that_never_reached_a_provider_rejects_it(
    request_lease, service, agent, read_lease
) -> None:
    """``REVOKED`` is an instruction to a teardown worker. Pointing one at a request that
    was still waiting would put a row in the destroy queue with nothing behind it."""
    outcome = await request_lease()
    view = await service.revoke(outcome.lease.id, agent, reason="changed my mind")
    assert view.state is LeaseState.REJECTED
    assert (await read_lease(outcome.lease.id)).next_attempt_at is None


async def test_revoking_needs_a_reason_and_an_owner(activate, service, other_agent) -> None:
    lease = await activate()
    with pytest.raises(InvalidRequest, match="needs a reason"):
        await service.revoke(lease.id, Caller.agent("agent-one"), reason=" ")
    with pytest.raises(LeaseNotFound):
        await service.revoke(lease.id, other_agent, reason="not mine")


async def test_a_finished_lease_cannot_be_revoked(request_lease, service, operator) -> None:
    outcome = await request_lease("forbidden", inputs={})
    with pytest.raises(ConflictingState, match="already rejected"):
        await service.revoke(outcome.lease.id, operator, reason="tidy up")


async def test_retrying_a_teardown_is_an_operator_action_on_an_orphan_only(
    activate, service, operator, agent, session: AsyncSession, read_lease
) -> None:
    """Orphans are deliberately not retried automatically: a loop that keeps calling a
    provider which keeps refusing hides the problem instead of surfacing it."""
    lease = await activate()
    with pytest.raises(NotAuthorized, match="operator token"):
        await service.retry_teardown(lease.id, agent)
    with pytest.raises(ConflictingState, match="only an orphaned lease"):
        await service.retry_teardown(lease.id, operator)

    row = await session.get(Lease, lease.id)
    assert row is not None
    row.state = LeaseState.ORPHANED.value
    row.next_attempt_at = None
    await session.commit()

    view = await service.retry_teardown(lease.id, operator)
    assert view.state is LeaseState.ORPHANED
    assert (await read_lease(lease.id)).next_attempt_at is not None


# --------------------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------------------


def test_backoff_grows_and_is_capped_and_jittered() -> None:
    """Jitter so a fleet of workers rate-limited at the same instant does not come back in
    lockstep and fail together again."""
    assert 2.5 <= backoff_seconds(1) <= 5.0
    assert 5.0 <= backoff_seconds(2) <= 10.0
    assert 300.0 <= backoff_seconds(99) <= 600.0
    assert len({round(backoff_seconds(3), 6) for _ in range(20)}) > 1


def test_an_external_name_fits_a_provider_name_limit() -> None:
    name = make_external_name("a-very-long-golden-path-identifier-indeed", "memory", "f" * 32)
    assert len(name) <= 63
    assert name.startswith(resource_prefix())


def test_an_external_name_is_derived_from_the_lease_id() -> None:
    first = make_external_name("sandbox", "memory", "3f2a9c1e-0000-0000-0000-000000000000")
    assert first == make_external_name("sandbox", "memory", "3f2a9c1e-0000-0000-0000-000000000000")
    assert first != make_external_name("sandbox", "memory", "aaaaaaaa-0000-0000-0000-000000000000")
    assert "sandbox" in first


def test_an_over_long_resource_prefix_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prefix that leaves no room would produce names the provider rejects, one lease at
    a time, with nothing pointing at the setting that caused it."""
    from bailment.engine.service import ServiceError

    monkeypatch.setenv("BAILMENT_RESOURCE_PREFIX", "x" * 60)
    with pytest.raises(ServiceError, match="shorter prefix"):
        make_external_name("sandbox", "memory", "3f2a9c1e")
