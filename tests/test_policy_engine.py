"""Policy chain evaluation.

The test this file exists for is
:func:`test_a_broken_rule_denies_instead_of_falling_through_to_a_later_allow`. Policy
chains are written as a series of narrowing denials ending in an allow, so a chain walker
that skipped a rule it could not evaluate would turn a deny into an allow -- silently, at
the moment the operator has least reason to look. It is the highest-consequence bug this
codebase could have, and it has a test with its own name so that deleting it is a visible
act.

Everything else here is about the two things a first-match-wins chain has to get right:
ordering, and the fact that :meth:`PolicyEngine.explain` and :meth:`PolicyEngine.evaluate`
must never be able to disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bailment.catalog.schema import PolicyRule
from bailment.policy.engine import (
    CONTEXT_KEYS,
    PolicyDecision,
    PolicyEngine,
    RequestContext,
    evaluate,
    explain,
)
from conftest import make_path


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


def context(**overrides: object) -> RequestContext:
    values: dict[str, object] = {
        "requester": "agent-one",
        "inputs": {"env": "dev", "name": "orders"},
        "is_agent": True,
        "golden_path": "postgres",
        "ttl_seconds": 3600,
        "at": datetime(2026, 7, 22, 13, 30, tzinfo=UTC),
    }
    values.update(overrides)
    return RequestContext(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# The one that matters
# --------------------------------------------------------------------------------------


def test_a_broken_rule_denies_instead_of_falling_through_to_a_later_allow(
    engine: PolicyEngine,
) -> None:
    """A rule that cannot be evaluated must deny the whole request.

    The chain below is the shape almost every real policy has: a narrowing refusal
    followed by a broad allow. The first rule refers to a name the context does not
    provide, which is what a typo, a renamed input or a rule copied between golden paths
    actually looks like. If the engine skipped it, the request would be *allowed* -- and
    the audit log would record a clean self-service approval for a request the operator
    intended to gate.
    """
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(
                when='environment == "prod"',  # the context name is 'env', not 'environment'
                effect="deny",
                reason="production is not self-service",
            ),
            PolicyRule(effect="allow", reason="everything else is self-service"),
        ],
    )

    decision = engine.evaluate(path, context())

    assert decision.effect == "deny"
    assert decision.allowed is False
    assert decision.rule_index == 0
    assert decision.error is not None
    assert "unknown name" in decision.error
    # The reason has to say which rule and why, because "denied" alone sends the operator
    # looking for a policy the requester violated, and there was none.
    assert "rule 0" in decision.reason
    assert "cannot be evaluated cannot be trusted" in decision.reason


def test_a_rule_that_fails_to_compile_also_denies(engine: PolicyEngine) -> None:
    """The loader refuses these at startup, so reaching one means the catalog was bypassed.

    Denying anyway is the point: the safe behaviour must not depend on a check somewhere
    else having run.
    """
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when="input.env ==", effect="deny", reason="broken"),
            PolicyRule(effect="allow", reason="self-service"),
        ],
    )
    decision = engine.evaluate(path, context())
    assert decision.effect == "deny"
    assert decision.rule_index == 0
    assert decision.error is not None


def test_a_broken_rule_after_a_matching_one_is_never_evaluated(engine: PolicyEngine) -> None:
    """First match wins, so a later broken rule cannot retroactively deny a decided request."""
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when='input.env == "dev"', effect="allow", reason="dev is fine"),
            PolicyRule(when="nosuchname == 1", effect="deny", reason="unreachable"),
            PolicyRule(effect="deny", reason="default"),
        ],
    )
    explanation = engine.explain(path, context())
    assert explanation.decision.effect == "allow"
    assert [trace.outcome for trace in explanation.traces] == [
        "matched",
        "not_evaluated",
        "not_evaluated",
    ]


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------


def test_first_match_wins(engine: PolicyEngine) -> None:
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when="is_agent", effect="require_approval", reason="agents ask first"),
            PolicyRule(when='input.env == "dev"', effect="allow", reason="dev is fine"),
            PolicyRule(effect="deny", reason="default"),
        ],
    )
    assert engine.evaluate(path, context()).effect == "require_approval"
    assert engine.evaluate(path, context(is_agent=False)).effect == "allow"
    assert engine.evaluate(path, context(is_agent=False, inputs={"env": "prod"})).effect == "deny"


def test_the_final_unconditional_rule_is_the_default(engine: PolicyEngine) -> None:
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when='input.env == "prod"', effect="deny", reason="no prod"),
            PolicyRule(effect="allow", reason="anything else"),
        ],
    )
    explanation = engine.explain(path, context())
    assert explanation.decision.effect == "allow"
    assert explanation.decision.rule_index == 1
    assert [trace.outcome for trace in explanation.traces] == ["not_matched", "matched"]


def test_a_chain_that_ends_without_a_decision_denies(engine: PolicyEngine) -> None:
    """Schema validation forbids this shape, so reaching it means the schema was bypassed.

    ``model_copy`` is how a test bypasses it; a future catalog format could do the same by
    accident. Denying rather than trusting the invariant is the whole habit of this module.
    """
    path = make_path("postgres").model_copy(
        update={"policy": [PolicyRule(when="false", effect="allow", reason="never matches")]}
    )
    decision = engine.evaluate(path, context())
    assert decision.effect == "deny"
    assert decision.rule_index is None
    assert decision.error == "policy chain ended without a decision"


def test_a_disabled_path_denies_without_evaluating_anything(engine: PolicyEngine) -> None:
    path = make_path("postgres", enabled=False, policy=[PolicyRule(effect="allow", reason="ok")])
    explanation = engine.explain(path, context())
    assert explanation.decision.effect == "deny"
    assert "disabled" in explanation.decision.reason
    assert [trace.outcome for trace in explanation.traces] == ["not_evaluated"]


# --------------------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------------------


def test_approvers_are_carried_only_on_a_require_approval_rule(engine: PolicyEngine) -> None:
    """Carrying an approver list on an allow would invite a caller to read consent into it."""
    gated = make_path(
        "postgres",
        policy=[
            PolicyRule(
                when="is_agent",
                effect="require_approval",
                reason="a human decides",
                approvers=["platform-team", "dana"],
            ),
            PolicyRule(effect="deny", reason="default"),
        ],
    )
    decision = engine.evaluate(gated, context())
    assert decision.needs_approval is True
    assert decision.approvers == ("platform-team", "dana")

    allowed = make_path(
        "postgres",
        policy=[
            PolicyRule(when="is_agent", effect="allow", reason="fine", approvers=["platform-team"]),
            PolicyRule(effect="deny", reason="default"),
        ],
    )
    assert engine.evaluate(allowed, context()).approvers == ()


def test_the_reason_is_carried_through_verbatim(engine: PolicyEngine) -> None:
    """It is shown to whoever was blocked, so paraphrasing it in transit is not allowed."""
    reason = "Branching production copies real customer data. Ask for staging instead."
    path = make_path("postgres", policy=[PolicyRule(effect="require_approval", reason=reason)])
    assert engine.evaluate(path, context()).reason == reason


def test_decision_predicates_agree_with_the_effect() -> None:
    assert PolicyDecision(effect="allow", reason="").allowed
    assert PolicyDecision(effect="deny", reason="").denied
    assert PolicyDecision(effect="require_approval", reason="").needs_approval


# --------------------------------------------------------------------------------------
# explain() and evaluate() are one walk
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inputs",
    [{"env": "dev"}, {"env": "prod"}, {"env": "staging"}, {}],
)
def test_explain_returns_the_decision_evaluate_would(
    engine: PolicyEngine, inputs: dict[str, str]
) -> None:
    """A second implementation of the walk is a second policy engine.

    The day the two disagree is the day the dashboard starts lying to an operator about
    why an agent was allowed to do something.
    """
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when='input.env == "prod"', effect="deny", reason="no prod"),
            PolicyRule(when='input.env == "dev"', effect="allow", reason="dev is fine"),
            PolicyRule(effect="require_approval", reason="anything else needs a person"),
        ],
    )
    ctx = context(inputs=inputs)
    assert engine.explain(path, ctx).decision == engine.evaluate(path, ctx)


def test_the_trace_records_every_rule_and_its_outcome(engine: PolicyEngine) -> None:
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when='input.env == "prod"', effect="deny", reason="no prod"),
            PolicyRule(when="nosuchname == 1", effect="deny", reason="broken"),
            PolicyRule(effect="allow", reason="default"),
        ],
    )
    explanation = engine.explain(path, context())
    assert explanation.golden_path_id == "postgres"
    assert [trace.index for trace in explanation.traces] == [0, 1, 2]
    assert [trace.outcome for trace in explanation.traces] == [
        "not_matched",
        "error",
        "not_evaluated",
    ]
    assert explanation.traces[0].decided is False
    assert explanation.traces[1].decided is True
    assert explanation.traces[1].error is not None
    assert explanation.traces[1].when == "nosuchname == 1"


def test_module_level_helpers_use_the_default_engine() -> None:
    path = make_path("postgres", policy=[PolicyRule(effect="allow", reason="ok")])
    assert evaluate(path, context()).effect == "allow"
    assert explain(path, context()).decision.effect == "allow"


# --------------------------------------------------------------------------------------
# The request context
# --------------------------------------------------------------------------------------


def test_the_context_exposes_exactly_the_documented_names() -> None:
    """``CONTEXT_KEYS`` is the user-facing API of the policy language.

    Adding or removing one is a breaking change to every golden path in every deployment,
    so the set is pinned. ``env`` is the conditional member: it appears only when the
    request supplied an ``env`` input.
    """
    with_env = context().as_mapping()
    assert set(with_env) == set(CONTEXT_KEYS)

    without_env = context(inputs={"name": "orders"}).as_mapping()
    assert set(without_env) == set(CONTEXT_KEYS) - {"env"}


def test_env_is_an_alias_for_the_input_of_the_same_name() -> None:
    mapping = context(inputs={"env": "staging"}).as_mapping()
    assert mapping["env"] == "staging"
    assert mapping["input"]["env"] == "staging"


def test_hour_and_weekday_are_utc() -> None:
    """Never local time: one broker, operators in three timezones, one policy file."""
    mapping = context(at=datetime(2026, 7, 26, 23, 15, tzinfo=UTC)).as_mapping()
    assert mapping["hour_utc"] == 23
    assert mapping["weekday"] == 6  # a Sunday; the weekend is weekday >= 5


def test_a_naive_decision_time_is_refused() -> None:
    """A programming error in the caller, not a policy failure.

    Failing closed here would hide a bug that makes every time-based rule wrong by the
    host's offset, which is exactly the kind of wrong nobody notices.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        context(at=datetime(2026, 7, 22, 13, 30)).as_mapping()


def test_as_mapping_hands_out_a_fresh_copy_every_time() -> None:
    """Nothing the evaluator touches may reach back into the caller's request object."""
    ctx = context()
    first = ctx.as_mapping()
    second = ctx.as_mapping()
    assert first == second
    assert first is not second
    first["input"]["env"] = "tampered"
    assert ctx.inputs["env"] == "dev"
    assert ctx.as_mapping()["input"]["env"] == "dev"


def test_a_mapping_may_be_passed_instead_of_a_request_context(engine: PolicyEngine) -> None:
    """The dashboard's what-if evaluation runs without a database or a real request."""
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when='env == "prod"', effect="deny", reason="no prod"),
            PolicyRule(effect="allow", reason="default"),
        ],
    )
    assert engine.evaluate(path, {"env": "prod"}).effect == "deny"
    assert engine.evaluate(path, {"env": "dev"}).effect == "allow"


# --------------------------------------------------------------------------------------
# Startup validation
# --------------------------------------------------------------------------------------


def test_validate_path_names_the_rules_that_will_not_compile(engine: PolicyEngine) -> None:
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when='env == "prod"', effect="deny", reason="fine"),
            PolicyRule(when="input.__class__", effect="deny", reason="hostile"),
            PolicyRule(when="1 +", effect="deny", reason="malformed"),
            PolicyRule(effect="allow", reason="default"),
        ],
    )
    problems = engine.validate_path(path)
    assert len(problems) == 2
    assert problems[0].startswith("policy rule 1:")
    assert problems[1].startswith("policy rule 2:")


def test_validate_path_is_quiet_about_a_sound_chain(engine: PolicyEngine) -> None:
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when="ttl_seconds > 129600", effect="require_approval", reason="long"),
            PolicyRule(effect="allow", reason="default"),
        ],
    )
    assert engine.validate_path(path) == []


def test_validate_path_does_not_catch_a_name_that_only_fails_at_runtime(
    engine: PolicyEngine,
) -> None:
    """A misspelled context name compiles fine; it is the *evaluation* that fails.

    Worth stating out loud, because it is why the deny-on-error behaviour has to exist at
    all: startup validation cannot see this one, so the request path must.
    """
    path = make_path(
        "postgres",
        policy=[
            PolicyRule(when="environment == 1", effect="deny", reason="typo"),
            PolicyRule(effect="allow", reason="default"),
        ],
    )
    assert engine.validate_path(path) == []
    assert engine.evaluate(path, context()).effect == "deny"
