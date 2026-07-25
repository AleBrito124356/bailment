"""The policy expression sandbox, tested as an attacker would.

Anyone who can influence a ``when`` expression, or trick the tree-walker into touching a
Python object it was not handed, owns the broker. So the rejection tests here assert two
separate things about every hostile input:

1. :func:`compile_expression` **raises**. Not "returns something falsy" -- an expression
   that compiles and evaluates to ``False`` is a rule that did not match, which is a
   completely different outcome from a rule that must never exist. A sandbox that silently
   treated ``__class__`` as an unmatched rule would look identical in every log.
2. :func:`evaluate_expression` reports ``ok=False``. That is the total path the policy
   engine actually calls, and it is the one that turns into a denial.

The second half of the file is about what error messages may contain. The policy context
is documented as secret-free, but "documented as" is not "guaranteed to be", and a failure
reason is written to the audit log and shown to the requester. Every rejection names
constructs, identifiers and type names -- never a value.
"""

from __future__ import annotations

import pytest

from bailment.policy.evaluator import (
    HELPER_NAMES,
    MAX_DEPTH,
    MAX_EXPRESSION_LENGTH,
    MAX_NODES,
    DisallowedConstruct,
    ExpressionSyntaxError,
    ExpressionTooComplex,
    PolicyExpressionError,
    compile_expression,
    evaluate_expression,
    validate_expression,
)

CONTEXT: dict[str, object] = {
    "input": {"env": "prod", "name": "orders", "size": 4, "tags": ["a", "b"]},
    "env": "prod",
    "requester": "agent-one",
    "on_behalf_of": None,
    "is_agent": True,
    "golden_path": "postgres",
    "ttl_seconds": 14400,
    "estimated_monthly_cost_usd": 102.2,
    "estimated_hourly_cost_usd": 0.14,
    "active_leases_for_requester": 2,
    "hour_utc": 13,
    "weekday": 2,
}


# --------------------------------------------------------------------------------------
# Sandbox escapes
# --------------------------------------------------------------------------------------

#: Every one of these is a documented way out of an ``eval``-based sandbox, or a construct
#: whose presence would mean the grammar had quietly widened.
ESCAPES = [
    pytest.param("__class__", id="bare-dunder-name"),
    pytest.param("__builtins__", id="builtins"),
    pytest.param("__import__", id="import-builtin"),
    pytest.param('__import__("os")', id="import-call"),
    pytest.param("input.__class__", id="dunder-attribute"),
    pytest.param("input.__globals__", id="globals-attribute"),
    pytest.param("input.__class__.__bases__", id="class-bases-chain"),
    pytest.param("().__class__.__bases__", id="empty-tuple-class-bases"),
    pytest.param("().__class__.__bases__[0].__subclasses__()", id="subclasses"),
    pytest.param('"".__class__', id="str-class"),
    pytest.param("input.__len__", id="dunder-method"),
    pytest.param("requester.__doc__", id="trailing-dunder"),
    pytest.param("input._private", id="single-underscore-is-fine-syntactically"),
]


@pytest.mark.parametrize("source", ESCAPES[:-1])
def test_sandbox_escapes_are_rejected_at_compile_time(source: str) -> None:
    with pytest.raises(DisallowedConstruct):
        compile_expression(source)


@pytest.mark.parametrize("source", ESCAPES[:-1])
def test_sandbox_escapes_also_fail_the_total_path(source: str) -> None:
    """The engine never calls ``compile_expression`` directly; it calls this.

    A construct rejected at compile time has to surface as ``ok=False`` here, because that
    is what becomes a denial. If only the raising path were tested, a future refactor that
    swallowed the exception would pass every test in the block above.
    """
    outcome = evaluate_expression(source, CONTEXT)
    assert outcome.ok is False
    assert outcome.value is False
    assert outcome.matched is False
    assert outcome.error


def test_a_single_underscore_attribute_is_not_a_dunder_but_still_fails_on_a_string() -> None:
    """``_private`` is legal syntax and still cannot reach anything.

    Attribute access is a dict lookup, so this is a missing-key error rather than a route
    to an implementation detail. Worth pinning: the dunder ban is a defence in depth, not
    the mechanism.
    """
    compiled = compile_expression("input._private")
    outcome = compiled.evaluate(CONTEXT)
    assert outcome.ok is False
    assert "_private" in (outcome.error or "")


#: Constructs outside the grammar. The message must name the construct, because the reader
#: is a platform engineer whose rule was refused at startup.
DISALLOWED_CONSTRUCTS = [
    pytest.param("(lambda: True)()", "lambda", id="lambda"),
    pytest.param("[x for x in input]", "list comprehension", id="list-comprehension"),
    pytest.param("{x for x in input}", "set comprehension", id="set-comprehension"),
    pytest.param("{k: v for k, v in input}", "dict comprehension", id="dict-comprehension"),
    pytest.param("(x for x in input)", "generator expression", id="generator"),
    pytest.param('f"{requester}" == "x"', "f-string", id="f-string"),
    pytest.param("(matched := true)", "walrus", id="walrus"),
    pytest.param('any_of(env, *["dev"])', "starred", id="starred-argument"),
    pytest.param('{"a": 1} == input', "dict display", id="dict-display"),
    pytest.param('requester[0:2] == "ag"', "slice", id="slice"),
    pytest.param("ttl_seconds + 1 > 2", "arithmetic", id="binop-add"),
    pytest.param('"x" * 1000000 == "y"', "arithmetic", id="binop-multiply"),
    pytest.param("2 ** 999999999 > 1", "arithmetic", id="binop-power"),
    pytest.param("true if is_agent else false", "conditional expression", id="ifexp"),
    pytest.param("env is None", "'is'", id="is"),
    pytest.param("env is not None", "'is not'", id="is-not"),
    pytest.param("~ttl_seconds > 0", "not allowed", id="invert"),
    pytest.param('b"bytes" == b"bytes"', "bytes literal", id="bytes-literal"),
]


@pytest.mark.parametrize(("source", "expected"), DISALLOWED_CONSTRUCTS)
def test_constructs_outside_the_grammar_are_rejected_by_name(source: str, expected: str) -> None:
    with pytest.raises(PolicyExpressionError) as caught:
        compile_expression(source)
    assert expected in str(caught.value)
    assert evaluate_expression(source, CONTEXT).ok is False


#: Callables. Only the seven helpers exist; everything else is refused, including the
#: method-call shape that a rule author's muscle memory produces.
CALLS = [
    pytest.param('open("/etc/passwd")', id="open"),
    pytest.param('eval("1")', id="eval"),
    pytest.param('exec("x=1")', id="exec"),
    pytest.param("print(1)", id="print"),
    pytest.param('getattr(input, "env")', id="getattr"),
    pytest.param("type(input) == 1", id="type"),
    pytest.param('input.get("env") == "prod"', id="method-call-on-mapping"),
    pytest.param('env.startswith("p")', id="method-call-on-string"),
    pytest.param('len(input)("x")', id="call-a-call-result"),
]


@pytest.mark.parametrize("source", CALLS)
def test_calls_to_anything_but_a_helper_are_rejected(source: str) -> None:
    with pytest.raises(DisallowedConstruct) as caught:
        compile_expression(source)
    message = str(caught.value)
    assert "helper" in message or "unknown function" in message
    assert evaluate_expression(source, CONTEXT).ok is False


def test_helper_names_are_exactly_the_seven() -> None:
    """Pinned because a rule file is written against this list and nothing else."""
    assert {
        "len",
        "lower",
        "upper",
        "startswith",
        "endswith",
        "matches",
        "any_of",
    } == HELPER_NAMES


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("len()", id="too-few"),
        pytest.param("len(env, env)", id="too-many"),
        pytest.param("startswith(env)", id="startswith-one-argument"),
        pytest.param("any_of(env)", id="any-of-one-argument"),
    ],
)
def test_helper_arity_is_checked_at_compile_time(source: str) -> None:
    with pytest.raises(DisallowedConstruct) as caught:
        compile_expression(source)
    assert "argument" in str(caught.value)


def test_helpers_reject_keyword_arguments() -> None:
    with pytest.raises(DisallowedConstruct) as caught:
        compile_expression('startswith(env, prefix="p")')
    assert "keyword" in str(caught.value)


# --------------------------------------------------------------------------------------
# Ceilings
# --------------------------------------------------------------------------------------


def test_an_oversized_expression_is_refused_before_it_reaches_the_parser() -> None:
    source = '"' + "a" * MAX_EXPRESSION_LENGTH + '" == env'
    with pytest.raises(ExpressionTooComplex) as caught:
        compile_expression(source)
    assert str(MAX_EXPRESSION_LENGTH) in str(caught.value)


def test_too_many_nodes_is_refused() -> None:
    source = " or ".join(["1 == 1"] * 100)
    assert len(source) < MAX_EXPRESSION_LENGTH
    with pytest.raises(ExpressionTooComplex) as caught:
        compile_expression(source)
    assert str(MAX_NODES) in str(caught.value)


def test_too_deep_a_nesting_is_refused() -> None:
    source = "not " * (MAX_DEPTH + 5) + "true"
    with pytest.raises(ExpressionTooComplex) as caught:
        compile_expression(source)
    assert str(MAX_DEPTH) in str(caught.value)


def test_deeply_parenthesised_input_cannot_take_the_parser_down() -> None:
    """Parentheses create no AST nodes, so no node budget can see this coming.

    Depending on the CPython version the parser answers with a ``SyntaxError`` about
    nesting or dies with a ``RecursionError``; the point of the test is that both arrive
    as a :class:`PolicyExpressionError` rather than as an unhandled exception on the
    request path, so it asserts the base class deliberately.
    """
    source = "(" * 400 + "1 == 1" + ")" * 400
    assert len(source) < MAX_EXPRESSION_LENGTH
    with pytest.raises(PolicyExpressionError):
        compile_expression(source)
    assert evaluate_expression(source, CONTEXT).ok is False


def test_an_oversized_display_is_refused() -> None:
    source = "env in [" + ", ".join(f'"{index}"' for index in range(150)) + "]"
    with pytest.raises(ExpressionTooComplex) as caught:
        compile_expression(source)
    assert "elements" in str(caught.value)


@pytest.mark.parametrize("source", ["", "   ", "\n\t "])
def test_an_empty_expression_is_a_syntax_error_with_advice(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        compile_expression(source)
    assert "unconditional" in str(caught.value)


@pytest.mark.parametrize("source", ["1 +", "and env", "env ==", "((", "a b c"])
def test_malformed_expressions_are_syntax_errors(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        compile_expression(source)


def test_matches_refuses_a_pathological_pattern_length() -> None:
    outcome = evaluate_expression(f'matches(env, "{"a" * 300}")', CONTEXT)
    assert outcome.ok is False
    assert "pattern is longer" in (outcome.error or "")


def test_matches_refuses_an_oversized_subject() -> None:
    context = {**CONTEXT, "requester": "x" * 5000}
    outcome = evaluate_expression('matches(requester, "x+")', context)
    assert outcome.ok is False
    assert "subject" in (outcome.error or "")


def test_matches_reports_an_invalid_regular_expression() -> None:
    outcome = evaluate_expression('matches(env, "[unclosed")', CONTEXT)
    assert outcome.ok is False
    assert "not a valid regular expression" in (outcome.error or "")


# --------------------------------------------------------------------------------------
# The grammar that does exist
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('env == "prod"', True, id="equality"),
        pytest.param('env != "prod"', False, id="inequality"),
        pytest.param('input.env == "prod"', True, id="attribute-is-a-key-lookup"),
        pytest.param('input["env"] == "prod"', True, id="subscript"),
        pytest.param("input.size < 10", True, id="less-than"),
        pytest.param("input.size <= 4", True, id="less-or-equal"),
        pytest.param("ttl_seconds > 3600", True, id="greater-than"),
        pytest.param("1 < input.size < 10", True, id="chained-comparison"),
        pytest.param("1 < input.size < 3", False, id="chained-comparison-false"),
        pytest.param('"env" in input', True, id="in-mapping-tests-keys"),
        pytest.param('"nope" in input', False, id="in-mapping-missing-key"),
        pytest.param('"a" in input.tags', True, id="in-list"),
        pytest.param('"ro" in env', True, id="in-string"),
        pytest.param('env not in ["dev", "staging"]', True, id="not-in"),
        pytest.param('env in {"prod", "staging"}', True, id="in-set-display"),
        pytest.param('env in ("prod",)', True, id="in-tuple-display"),
        pytest.param("is_agent and ttl_seconds > 60", True, id="and"),
        pytest.param("is_agent or false", True, id="or"),
        pytest.param("not is_agent", False, id="not"),
        pytest.param("-ttl_seconds < 0", True, id="unary-minus"),
        pytest.param("estimated_hourly_cost_usd > 0.10", True, id="float-comparison"),
        pytest.param("on_behalf_of == null", True, id="yaml-null"),
        pytest.param("is_agent == true", True, id="yaml-true"),
        pytest.param("is_agent == false", False, id="yaml-false"),
        pytest.param("is_agent == True", True, id="python-true"),
        pytest.param("on_behalf_of == None", True, id="python-none"),
        pytest.param("len(input.name) == 6", True, id="len-string"),
        pytest.param("len(input.tags) == 2", True, id="len-list"),
        pytest.param("len(input) == 4", True, id="len-mapping"),
        pytest.param('lower(golden_path) == "postgres"', True, id="lower"),
        pytest.param('upper(env) == "PROD"', True, id="upper"),
        pytest.param('startswith(requester, "agent")', True, id="startswith"),
        pytest.param('startswith(requester, ["ci", "agent"])', True, id="startswith-many"),
        pytest.param('endswith(requester, "-one")', True, id="endswith"),
        pytest.param('matches(input.name, "[a-z]+")', True, id="matches"),
        pytest.param('matches(input.name, "orde")', False, id="matches-is-a-fullmatch"),
        pytest.param('any_of(env, "dev", "prod")', True, id="any-of-varargs"),
        pytest.param("any_of(env, input.tags)", False, id="any-of-collection"),
        pytest.param("hour_utc >= 0 and weekday < 7", True, id="clock-names"),
        pytest.param("active_leases_for_requester >= 2", True, id="quota-name"),
    ],
)
def test_the_supported_grammar_evaluates(source: str, expected: bool) -> None:
    outcome = evaluate_expression(source, CONTEXT)
    assert outcome.ok is True, outcome.error
    assert outcome.value is expected
    assert outcome.matched is expected


def test_short_circuit_makes_the_optional_input_idiom_work() -> None:
    """``"env" in input and input.env == "prod"`` is the supported optional-key pattern.

    Without short-circuiting, the right-hand side would raise an unknown-key error on a
    request that simply did not supply ``env``, and the rule -- and therefore the whole
    request -- would be denied for asking a reasonable question.
    """
    context = {**CONTEXT, "input": {"name": "orders"}}
    outcome = evaluate_expression('"env" in input and input.env == "prod"', context)
    assert outcome.ok is True
    assert outcome.value is False


def test_or_short_circuits_before_a_failing_operand() -> None:
    outcome = evaluate_expression("is_agent or nosuchname", CONTEXT)
    assert outcome.ok is True
    assert outcome.value is True


def test_yaml_literal_names_shadow_a_context_key_of_the_same_name() -> None:
    """Documented behaviour, pinned. No real context key is called ``true``."""
    outcome = evaluate_expression("true", {**CONTEXT, "true": False})
    assert outcome.ok is True
    assert outcome.value is True


def test_a_compiled_expression_is_reusable_and_holds_no_state() -> None:
    compiled = compile_expression('env == "prod"')
    assert compiled.evaluate(CONTEXT).value is True
    assert compiled.evaluate({**CONTEXT, "env": "dev"}).value is False
    assert compiled.evaluate(CONTEXT).value is True
    assert compiled.node_count > 0
    assert compiled.source == 'env == "prod"'


def test_compilation_is_cached_by_source_text() -> None:
    assert compile_expression('env == "dev"') is compile_expression('env == "dev"')
    assert compile_expression('env == "dev"') is not compile_expression('env == "staging"')


def test_validate_expression_reports_a_problem_or_none() -> None:
    assert validate_expression('env == "prod"') is None
    problem = validate_expression("input.__class__")
    assert problem is not None
    assert "dunder" in problem


# --------------------------------------------------------------------------------------
# Runtime refusals
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        pytest.param("nosuchname == 1", "unknown name", id="unknown-name"),
        pytest.param("input.nosuchkey == 1", "no key", id="unknown-key"),
        pytest.param("requester.anything == 1", "dotted access", id="attribute-on-a-string"),
        pytest.param("ttl_seconds.anything == 1", "dotted access", id="attribute-on-an-int"),
        pytest.param("is_agent[0]", "cannot be indexed", id="subscript-a-bool"),
        pytest.param('input.tags["a"]', "whole number", id="list-indexed-by-string"),
        pytest.param("input.tags[99]", "out of range", id="index-out-of-range"),
        pytest.param("env < 5", "cannot order", id="order-string-against-int"),
        pytest.param("5 in ttl_seconds", "'in' needs", id="in-on-an-int"),
        pytest.param("5 in env", "needs a string", id="in-string-with-non-string"),
        pytest.param("len(is_agent) == 1", "len() needs", id="len-of-a-bool"),
        pytest.param('lower(ttl_seconds) == "x"', "needs a string", id="lower-of-an-int"),
        pytest.param('matches(ttl_seconds, "1")', "needs a string", id="matches-non-string"),
        pytest.param("matches(env, 5)", "regular expression string", id="matches-non-pattern"),
        pytest.param("env and true", "needs true or false", id="and-with-a-string"),
        pytest.param("not env", "needs true or false", id="not-with-a-string"),
        pytest.param("-env < 0", "unary minus needs a number", id="minus-a-string"),
    ],
)
def test_runtime_refusals_are_total_and_explain_themselves(source: str, fragment: str) -> None:
    outcome = evaluate_expression(source, CONTEXT)
    assert outcome.ok is False
    assert outcome.value is False
    assert fragment in (outcome.error or "")


def test_a_non_boolean_result_is_a_failure_not_a_truthy_match() -> None:
    """A rule is a yes/no question. Guessing at truthiness is how a rule silently inverts."""
    for source in ("requester", "input", "ttl_seconds", "len(input.name)", "input.tags"):
        outcome = evaluate_expression(source, CONTEXT)
        assert outcome.ok is False, source
        assert "must produce true or false" in (outcome.error or "")


def test_an_unknown_name_lists_the_available_names_but_not_their_values() -> None:
    outcome = evaluate_expression("nosuchname == 1", CONTEXT)
    error = outcome.error or ""
    assert "'requester'" in error
    assert "agent-one" not in error


def test_no_error_message_ever_contains_a_context_value() -> None:
    """The highest-value property in this file after the escapes themselves.

    A failure reason is written to the audit log and shown verbatim to the requester. The
    context is documented as secret-free -- but a deployment that adds a name to it, or a
    provider input that happens to carry a token, must not turn a typo'd rule into a
    disclosure.
    """
    marker = "s3cr3t-do-not-log"
    context = {
        "input": {"password": marker, "name": marker},
        "requester": marker,
        "is_agent": True,
        "ttl_seconds": 60,
    }
    sources = [
        "nosuchname == 1",
        "input.nosuchkey == 1",
        "requester.anything == 1",
        "requester",
        "input",
        "requester < 5",
        "not requester",
        "len(is_agent) == 1",
        "input.password[99]",
        'matches(requester, "[")',
        "requester and true",
    ]
    for source in sources:
        outcome = evaluate_expression(source, context)
        assert outcome.ok is False, source
        assert marker not in (outcome.error or ""), source


def test_the_evaluator_never_raises_whatever_it_is_handed() -> None:
    """Totality is the contract. The engine turns ``ok=False`` into a denial and relies
    on there being no other outcome to handle."""

    class Hostile:
        """A context value that explodes on every operation a rule might attempt."""

        def __eq__(self, other: object) -> bool:
            raise RuntimeError("boom: this message must not escape")

        def __hash__(self) -> int:
            raise RuntimeError("boom: this message must not escape")

    context = {"input": {"thing": Hostile()}, "hostile": Hostile()}
    for source in ('hostile == "x"', 'input.thing == "x"', 'hostile in ["x"]'):
        outcome = evaluate_expression(source, context)
        assert outcome.ok is False, source
        # Only the exception type is reported: an arbitrary exception's message can carry
        # a value out of the context and into the audit log.
        assert "RuntimeError" in (outcome.error or "")
        assert "boom" not in (outcome.error or "")
