"""Policy: deciding whether a request becomes a resource.

Two pieces, deliberately separable. :mod:`bailment.policy.evaluator` is a restricted
expression language with no knowledge of leases, providers or requests -- it turns a
string and a dict into true, false, or a typed failure, and it is the part that has to
be right for the broker to be safe at all. :mod:`bailment.policy.engine` knows about
golden paths and walks their rule chains, and is the part that has to be right for the
broker to be *predictable*.

The split matters because the evaluator can then be tested adversarially in isolation:
every rejection case is a unit test that needs no database, no golden path and no
request.

Import from here rather than from the submodules; the module layout is not part of the
contract, these names are.
"""

from __future__ import annotations

from bailment.policy.engine import (
    CONTEXT_KEYS,
    PolicyDecision,
    PolicyEngine,
    PolicyExplanation,
    RequestContext,
    RuleOutcome,
    RuleTrace,
    evaluate,
    explain,
)
from bailment.policy.evaluator import (
    HELPER_NAMES,
    MAX_DEPTH,
    MAX_EXPRESSION_LENGTH,
    MAX_NODES,
    CompiledExpression,
    DisallowedConstruct,
    ExpressionOutcome,
    ExpressionRuntimeError,
    ExpressionSyntaxError,
    ExpressionTooComplex,
    PolicyExpressionError,
    compile_expression,
    evaluate_expression,
    validate_expression,
)

__all__ = [
    "CONTEXT_KEYS",
    "HELPER_NAMES",
    "MAX_DEPTH",
    "MAX_EXPRESSION_LENGTH",
    "MAX_NODES",
    "CompiledExpression",
    "DisallowedConstruct",
    "ExpressionOutcome",
    "ExpressionRuntimeError",
    "ExpressionSyntaxError",
    "ExpressionTooComplex",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyExplanation",
    "PolicyExpressionError",
    "RequestContext",
    "RuleOutcome",
    "RuleTrace",
    "compile_expression",
    "evaluate",
    "evaluate_expression",
    "explain",
    "validate_expression",
]
