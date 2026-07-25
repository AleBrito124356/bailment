"""Policy chain evaluation.

A golden path carries an ordered list of rules. This module walks that list top to
bottom, evaluates each rule's ``when`` expression against the request, and returns the
effect of the first rule that matches. First match wins because it is the only ordering
semantics people predict correctly under pressure; a "most specific wins" or "deny
overrides" scheme reads fine in a design document and produces surprises in an incident.

**The failure mode this module exists to avoid.** If a rule's expression cannot be
evaluated -- a typo in a name, a golden path that stopped supplying an input, a
comparison between a string and a number -- the tempting behaviour is to log a warning,
skip the rule and carry on down the chain. That is catastrophic. Policy chains are
almost always written as a series of narrowing denials ending in an allow, so skipping a
broken rule turns a deny into an allow, silently, at exactly the moment the operator has
least reason to look. A rule that cannot be evaluated therefore denies the entire
request and names itself in the reason. Loud and wrong beats quiet and permissive.

**Why evaluate() and explain() share one code path.** :meth:`PolicyEngine.explain`
returns the same decision object :meth:`PolicyEngine.evaluate` would, plus the per-rule
trace behind it. Both call the same private walk. An explain implementation that
re-derives the decision separately is a second policy engine, and the day the two
disagree is the day the dashboard starts lying to the operator about why an agent was
allowed to do something.

The engine is pure: no I/O, no logging, no clock of its own. The caller supplies the
context, records the audit event and persists the decision. Keeping it pure is what
makes it testable exhaustively and what lets the dashboard run a what-if evaluation
without touching a database.

----

**The policy context.** These names are the user-facing API of the policy language and
changing one is a breaking change to every golden path in every deployment. All of them
are plain data -- strings, numbers, booleans and dicts. No object with behaviour ever
enters the context, and no secret value ever does either: bindings are encrypted and
are not created until well after the policy decision has been made.

``input``
    The validated request inputs, as a mapping. Write ``input.env`` or ``input["env"]``.
    Only keys the golden path's own JSON Schema declares can be present, because the
    schema is closed (``additionalProperties: false``) before it is ever used.

``env``
    Convenience alias for ``input.env``, present only when the request actually supplied
    an ``env`` input. If a path has no ``env`` input, referring to ``env`` is an unknown
    name and the request is denied -- which is the correct outcome for a rule written
    against a path it does not fit.

``requester``
    The principal that called the API. For agent traffic this is the agent's identity,
    not the human's.

``on_behalf_of``
    The human the agent is acting for, or ``null`` for a direct human request. Use this,
    not ``requester``, for anything about accountability.

``is_agent``
    True when the request arrived over MCP rather than from a human at the dashboard or
    the API. The single most useful predicate in the whole language: ``is_agent and
    env == "prod"`` is the rule almost every platform team wants first.

``golden_path``
    The golden path id, so a shared rule fragment can still discriminate.

``ttl_seconds``
    The *effective* lease duration in seconds, after the requested TTL has been clamped
    to the path's ``max_ttl``. Clamped rather than raw so that a rule like
    ``ttl_seconds > 14400`` is a statement about what will actually happen.

``estimated_monthly_cost_usd`` / ``estimated_hourly_cost_usd``
    From the golden path's cost model. Floats, so compare with care.

``active_leases_for_requester``
    How many live leases this requester already holds, across all paths. The blunt
    instrument that stops a looping agent from provisioning forty databases.

``hour_utc``
    Integer 0-23, in UTC. Never local time: a broker with operators in three timezones
    and one policy file has to pick one clock, and UTC is the one everybody can reason
    about from a log line.

``weekday``
    Integer 0-6 with Monday as 0, matching ``datetime.weekday()``. The weekend is
    ``weekday >= 5``.

Arithmetic is not available in the expression language, so anything a rule needs to
compare against has to arrive here precomputed. That is a deliberate trade; see
:mod:`bailment.policy.evaluator`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from bailment.catalog.schema import GoldenPath, PolicyEffect, PolicyRule
from bailment.models import utcnow
from bailment.policy.evaluator import ExpressionOutcome, evaluate_expression, validate_expression

__all__ = [
    "CONTEXT_KEYS",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyExplanation",
    "RequestContext",
    "RuleOutcome",
    "RuleTrace",
    "evaluate",
    "explain",
]

CONTEXT_KEYS: tuple[str, ...] = (
    "input",
    "env",
    "requester",
    "on_behalf_of",
    "is_agent",
    "golden_path",
    "ttl_seconds",
    "estimated_monthly_cost_usd",
    "estimated_hourly_cost_usd",
    "active_leases_for_requester",
    "hour_utc",
    "weekday",
)
"""Every name a policy expression may reference.

``env`` is the conditional one: it is present only when the request supplied an ``env``
input. See the module docstring.
"""

RuleOutcome = Literal["matched", "not_matched", "error", "not_evaluated"]
"""``not_evaluated`` means an earlier rule already won, so this one was never run."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything policy is allowed to know about a request.

    Built by the API layer, which is also responsible for having validated ``inputs``
    against the golden path's schema and for having clamped ``ttl_seconds`` to the
    path's ceiling *before* constructing this. Policy evaluates what will actually
    happen, not what was asked for.
    """

    requester: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    on_behalf_of: str | None = None
    is_agent: bool = False
    golden_path: str = ""
    ttl_seconds: int = 0
    estimated_monthly_cost_usd: float = 0.0
    estimated_hourly_cost_usd: float = 0.0
    active_leases_for_requester: int = 0
    at: datetime | None = None
    """Decision time, for ``hour_utc`` and ``weekday``. Defaults to now. Must be aware."""

    def as_mapping(self) -> dict[str, Any]:
        """Render to the flat mapping the evaluator resolves names from.

        A fresh dict every call, and ``input`` is a shallow copy, so nothing the
        evaluator touches can reach back into the caller's request object.
        """
        moment = self.at if self.at is not None else utcnow()
        if moment.tzinfo is None:
            raise ValueError(
                "RequestContext.at must be timezone-aware; a naive datetime here would "
                "silently shift hour_utc by the host's offset"
            )
        moment = moment.astimezone(UTC)

        inputs = dict(self.inputs)
        context: dict[str, Any] = {
            "input": inputs,
            "requester": self.requester,
            "on_behalf_of": self.on_behalf_of,
            "is_agent": self.is_agent,
            "golden_path": self.golden_path,
            "ttl_seconds": self.ttl_seconds,
            "estimated_monthly_cost_usd": self.estimated_monthly_cost_usd,
            "estimated_hourly_cost_usd": self.estimated_hourly_cost_usd,
            "active_leases_for_requester": self.active_leases_for_requester,
            "hour_utc": moment.hour,
            "weekday": moment.weekday(),
        }
        if "env" in inputs:
            context["env"] = inputs["env"]
        return context


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The answer. One of these is persisted on the lease row for every request."""

    effect: PolicyEffect
    reason: str
    """Shown verbatim to the requester and written to the audit log."""

    rule_index: int | None = None
    """Which rule decided. ``None`` when the engine denied before or beyond the chain."""

    approvers: tuple[str, ...] = ()
    """Principals allowed to approve. Empty with ``require_approval`` means any approver."""

    error: str | None = None
    """Set when the decision came from a failure rather than from a rule matching."""

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"

    @property
    def denied(self) -> bool:
        return self.effect == "deny"

    @property
    def needs_approval(self) -> bool:
        return self.effect == "require_approval"


@dataclass(frozen=True, slots=True)
class RuleTrace:
    """What happened at one rule. The dashboard renders these in order."""

    index: int
    when: str | None
    effect: PolicyEffect
    reason: str
    outcome: RuleOutcome
    error: str | None = None

    @property
    def decided(self) -> bool:
        return self.outcome in ("matched", "error")


@dataclass(frozen=True, slots=True)
class PolicyExplanation:
    """A decision plus the trace that produced it."""

    golden_path_id: str
    decision: PolicyDecision
    traces: tuple[RuleTrace, ...]


class PolicyEngine:
    """Evaluates a golden path's policy chain against a request.

    Stateless and safe to share. It is a class rather than a bare function so a
    deployment can subclass it for extra checks without the rest of the codebase having
    to learn about that; the module-level :func:`evaluate` and :func:`explain` wrap a
    default instance for callers that need neither.
    """

    def evaluate(
        self,
        path: GoldenPath,
        request_context: RequestContext | Mapping[str, Any],
    ) -> PolicyDecision:
        """Return the effect of the first matching rule, or a denial.

        Never raises for anything to do with the rules themselves. A malformed context
        object -- a naive ``datetime``, for instance -- is a programming error in the
        caller and still raises, because failing closed there would hide a bug that
        makes every subsequent time-based rule wrong.
        """
        return self._walk(path, request_context).decision

    def explain(
        self,
        path: GoldenPath,
        request_context: RequestContext | Mapping[str, Any],
    ) -> PolicyExplanation:
        """Return the decision together with a per-rule trace.

        Same walk, same result, no second implementation. Building the trace costs a
        handful of small objects, which is cheap enough that there is no reason to have
        an untraced fast path and therefore no reason for the two to ever diverge.
        """
        return self._walk(path, request_context)

    def validate_path(self, path: GoldenPath) -> list[str]:
        """Check every expression in ``path`` compiles. Empty list means the path is sound.

        Called by the catalog loader so a broken rule is a startup failure rather than a
        surprise denial in front of an agent that is mid-task.
        """
        problems: list[str] = []
        for index, rule in enumerate(path.policy):
            if rule.when is None:
                continue
            problem = validate_expression(rule.when)
            if problem is not None:
                problems.append(f"policy rule {index}: {problem}")
        return problems

    # --- internals ----------------------------------------------------------

    def _walk(
        self,
        path: GoldenPath,
        request_context: RequestContext | Mapping[str, Any],
    ) -> PolicyExplanation:
        context = (
            request_context.as_mapping()
            if isinstance(request_context, RequestContext)
            else dict(request_context)
        )

        if not path.enabled:
            # A disabled path should never have been offered in any catalog, so a
            # request for one means either a stale client or someone poking at ids.
            # Both deserve the same answer.
            return PolicyExplanation(
                golden_path_id=path.id,
                decision=PolicyDecision(
                    effect="deny",
                    reason=f"the golden path {path.id!r} is disabled and cannot be provisioned",
                ),
                traces=tuple(
                    _trace(index, rule, "not_evaluated") for index, rule in enumerate(path.policy)
                ),
            )

        traces: list[RuleTrace] = []
        decision: PolicyDecision | None = None

        for index, rule in enumerate(path.policy):
            if decision is not None:
                traces.append(_trace(index, rule, "not_evaluated"))
                continue

            if rule.when is None:
                traces.append(_trace(index, rule, "matched"))
                decision = _decide(index, rule)
                continue

            outcome: ExpressionOutcome = evaluate_expression(rule.when, context)
            if not outcome.ok:
                error = outcome.error or "unknown evaluation failure"
                traces.append(_trace(index, rule, "error", error))
                decision = PolicyDecision(
                    effect="deny",
                    reason=(
                        f"policy rule {index} of golden path {path.id!r} could not be "
                        f"evaluated ({error}); the request is denied because a policy "
                        f"that cannot be evaluated cannot be trusted"
                    ),
                    rule_index=index,
                    error=error,
                )
                continue

            if outcome.value:
                traces.append(_trace(index, rule, "matched"))
                decision = _decide(index, rule)
            else:
                traces.append(_trace(index, rule, "not_matched"))

        if decision is None:
            # GoldenPath validation requires a final unconditional rule, so reaching
            # here means the schema was bypassed -- a hand-built object in a test, or a
            # future change to the catalog. Deny rather than trust the invariant.
            decision = PolicyDecision(
                effect="deny",
                reason=(
                    f"no policy rule matched for golden path {path.id!r} and it has no "
                    f"unconditional default rule; denying by default"
                ),
                error="policy chain ended without a decision",
            )

        return PolicyExplanation(golden_path_id=path.id, decision=decision, traces=tuple(traces))


def _decide(index: int, rule: PolicyRule) -> PolicyDecision:
    return PolicyDecision(
        effect=rule.effect,
        reason=rule.reason,
        rule_index=index,
        # Approvers only mean anything on a require_approval rule; carrying them on an
        # allow would invite a caller to read consent into a list that was never one.
        approvers=tuple(rule.approvers) if rule.effect == "require_approval" else (),
    )


def _trace(
    index: int, rule: PolicyRule, outcome: RuleOutcome, error: str | None = None
) -> RuleTrace:
    return RuleTrace(
        index=index,
        when=rule.when,
        effect=rule.effect,
        reason=rule.reason,
        outcome=outcome,
        error=error,
    )


_DEFAULT_ENGINE = PolicyEngine()


def evaluate(
    path: GoldenPath, request_context: RequestContext | Mapping[str, Any]
) -> PolicyDecision:
    """Evaluate with the default engine."""
    return _DEFAULT_ENGINE.evaluate(path, request_context)


def explain(
    path: GoldenPath, request_context: RequestContext | Mapping[str, Any]
) -> PolicyExplanation:
    """Explain with the default engine."""
    return _DEFAULT_ENGINE.explain(path, request_context)
