"""A restricted expression language for policy rules.

Policy rules decide whether an autonomous agent gets a real cloud resource. That makes
this file the highest-value target in the codebase: anyone who can influence a rule
expression, or trick the evaluator into touching a Python object it was not meant to
touch, owns the broker. The design below is therefore paranoid on purpose.

**Why not ``eval``.** ``eval`` with a stripped ``__builtins__`` is not a sandbox, it is a
puzzle, and the puzzle has been solved publicly many times over -- one attribute hop from
any object reaches ``__class__``, then ``__subclasses__``, then a file handle. There is no
configuration of ``eval`` that is safe to point at a string, so no string is ever passed
to it. The expression is parsed to an AST and interpreted by the tree-walker in this
module, which can only reach values the caller explicitly put in the context dict.

**Why not a third-party expression engine.** CEL, JMESPath, simpleeval and friends are
each another dependency with another CVE feed, and the ones implemented in Python
mostly reduce to ``eval`` or to ``getattr`` chains anyway. The language bailment actually
needs is about forty lines of comparison and containment. Owning it is cheaper than
auditing someone else's.

**Why attribute access is a dict lookup.** ``input.env`` is sugar for ``input["env"]``.
``getattr`` is never called anywhere in this file. That single rule is what makes the
sandbox hold: with ``getattr``, every value reachable from the context becomes a doorway
to its type, its module and eventually the interpreter; without it, a value is only ever
data. Attribute access on anything that is not a Mapping is an error, not a fallback.

**Why there is no arithmetic.** ``ast.BinOp`` is rejected outright. It is not just that
policies rarely need it -- ``2 ** 999999999`` and ``"x" * 10**9`` are a hang and an OOM
written in four characters each, evaluated before any node budget can react. Comparing
against a precomputed context value costs the rule author nothing and removes the whole
class.

**Why the caps exist.** A rule is written by a platform engineer, so this is not a
defence against an attacker with commit access -- it is a defence against the ordinary
accident of a generated or copy-pasted expression taking the request path down with it.
Expression length, node count and nesting depth are all bounded, and deeply parenthesised
input that would blow the CPython parser's stack is caught at parse time rather than
becoming a RecursionError somewhere up the call chain.

**Why evaluation is total.** :meth:`CompiledExpression.evaluate` returns an
:class:`ExpressionOutcome` and never raises. The policy engine turns any failure into a
DENY. If failures propagated as exceptions, the correct handling of a broken rule would
depend on every call site remembering to catch them, and the day one forgets, a broken
rule becomes an allow.

**Why no value ever appears in an error message.** Messages name identifiers, keys and
type names, never the data behind them. The policy context is documented as
secret-free, but "documented as" is not "guaranteed to be", and a policy failure reason
is written to the audit log and shown to the requester.

The grammar, in full:

* literals: strings, integers, floats, and booleans/null in either the YAML spelling
  (``true``, ``false``, ``null``) or the Python one (``True``, ``False``, ``None``) --
  rules live in a YAML file, and a language that rejected ``true`` there would be a
  papercut in every policy anybody ever writes
* names, resolved only from the supplied context mapping
* ``input.env`` attribute access, meaning ``input["env"]``
* ``input["env"]`` and ``some_list[0]`` subscripts (no slices)
* comparisons ``== != < <= > >= in not in``, including chained forms
* ``and`` / ``or`` / ``not``, with strict boolean operands and short-circuit evaluation
* unary minus on numbers
* tuple, list and set displays
* the fixed helper set: ``len``, ``lower``, ``upper``, ``startswith``, ``endswith``,
  ``matches``, ``any_of``

Everything else -- lambdas, comprehensions, f-strings, walrus, starred arguments, dict
displays, conditional expressions, ``is``, dunder identifiers, calls to anything outside
the helper set -- is rejected at compile time with a message naming the construct.

Short-circuit evaluation is load-bearing rather than an optimisation: ``in`` on a mapping
tests keys, so ``"env" in input and input.env == "prod"`` is the supported way to write a
rule against an optional input without tripping the unknown-key error.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

__all__ = [
    "MAX_DEPTH",
    "MAX_EXPRESSION_LENGTH",
    "MAX_NODES",
    "CompiledExpression",
    "DisallowedConstruct",
    "ExpressionOutcome",
    "ExpressionRuntimeError",
    "ExpressionSyntaxError",
    "ExpressionTooComplex",
    "HELPER_NAMES",
    "PolicyExpressionError",
    "compile_expression",
    "evaluate_expression",
    "validate_expression",
]


# --- Resource ceilings ------------------------------------------------------------
#
# Chosen to be far above any rule a person would write by hand and far below anything
# that costs measurable time. If a real policy ever needs more than this, the rule
# wants splitting, not a bigger number.

MAX_EXPRESSION_LENGTH: Final[int] = 2_000
"""Characters. Checked before the parser is handed anything."""

MAX_NODES: Final[int] = 250
"""AST nodes. Also bounds how many helper calls one expression can make."""

MAX_DEPTH: Final[int] = 20
"""Nesting depth of the AST. Keeps the recursive walker's stack usage trivial."""

MAX_ELEMENTS: Final[int] = 100
"""Elements in a single tuple/list/set display."""

MAX_PATTERN_LENGTH: Final[int] = 256
"""Characters in a ``matches()`` pattern."""

MAX_MATCH_SUBJECT_LENGTH: Final[int] = 4_096
"""Characters of subject that ``matches()`` will look at before refusing."""

_EXPRESSION_CACHE_SIZE: Final[int] = 512
_PATTERN_CACHE_SIZE: Final[int] = 256
_SNIPPET_LENGTH: Final[int] = 80


class PolicyExpressionError(Exception):
    """Base class for every failure this module produces.

    Callers should catch this and nothing narrower unless they intend to distinguish
    "the rule is malformed" from "the rule did not fit the request".
    """


class ExpressionSyntaxError(PolicyExpressionError):
    """The source is not a parseable Python expression."""


class DisallowedConstruct(PolicyExpressionError):
    """The expression parsed but used a construct outside the grammar."""


class ExpressionTooComplex(PolicyExpressionError):
    """The expression exceeded a length, node-count or nesting ceiling."""


class ExpressionRuntimeError(PolicyExpressionError):
    """Evaluation could not produce a value: unknown name, wrong type, bad pattern."""


@dataclass(frozen=True, slots=True)
class ExpressionOutcome:
    """The total result of evaluating an expression.

    ``ok`` false means the rule could not be decided at all, which the policy engine
    treats as a denial of the entire request. It is deliberately not the same thing as
    ``ok`` true with ``value`` false -- "this rule does not apply" and "this rule is
    broken" must never be confusable.
    """

    ok: bool
    value: bool
    error: str | None = None

    @classmethod
    def success(cls, value: bool) -> ExpressionOutcome:
        return cls(ok=True, value=value, error=None)

    @classmethod
    def failure(cls, error: str) -> ExpressionOutcome:
        return cls(ok=False, value=False, error=error)

    @property
    def matched(self) -> bool:
        """True only when the expression evaluated cleanly and was true."""
        return self.ok and self.value


# --- Grammar tables ---------------------------------------------------------------

#: Helper name -> (minimum arity, maximum arity or None for variadic).
_HELPER_ARITY: Final[dict[str, tuple[int, int | None]]] = {
    "len": (1, 1),
    "lower": (1, 1),
    "upper": (1, 1),
    "startswith": (2, 2),
    "endswith": (2, 2),
    "matches": (2, 2),
    "any_of": (2, None),
}

HELPER_NAMES: Final[frozenset[str]] = frozenset(_HELPER_ARITY)
"""The complete set of callables reachable from an expression."""

#: Names that are literals rather than context lookups. ``True``, ``False`` and ``None``
#: already parse as constants; these are the YAML spellings, which is how a rule author
#: writing the surrounding file spells them and therefore how they will reach for them
#: inside a ``when``. They shadow any context key of the same name, which is fine: the
#: context is a fixed, documented set of names and none of them is called "true".
_LITERAL_NAMES: Final[dict[str, Any]] = {"true": True, "false": False, "null": None}

#: Friendly names for the constructs worth rejecting by name rather than by class.
_CONSTRUCT_NAMES: Final[dict[type[ast.AST], str]] = {
    ast.Lambda: "a lambda",
    ast.ListComp: "a list comprehension",
    ast.SetComp: "a set comprehension",
    ast.DictComp: "a dict comprehension",
    ast.GeneratorExp: "a generator expression",
    ast.JoinedStr: "an f-string",
    ast.FormattedValue: "an f-string",
    ast.NamedExpr: "a walrus assignment",
    ast.Starred: "a starred argument",
    ast.Await: "an await",
    ast.Yield: "a yield",
    ast.YieldFrom: "a yield-from",
    ast.BinOp: "an arithmetic or bitwise operator",
    ast.IfExp: "a conditional expression",
    ast.Dict: "a dict display",
    ast.Slice: "a slice",
}

#: Extra guidance appended to a rejection, where there is an obvious rewrite.
_CONSTRUCT_HINTS: Final[dict[type[ast.AST], str]] = {
    ast.BinOp: (
        "arithmetic is not part of the policy language; compare against a value the "
        "broker already computed, such as ttl_seconds or estimated_hourly_cost_usd"
    ),
    ast.JoinedStr: "use == or matches() instead of building a string",
    ast.IfExp: "write two rules instead; the policy chain already means first match wins",
    ast.Dict: "use a list or tuple with 'in', or the any_of() helper",
    ast.Slice: "use startswith(), endswith() or matches()",
    ast.Lambda: "the policy language has no user-defined functions",
}

_COMPARE_OP_NAMES: Final[dict[type[ast.AST], str]] = {
    ast.Is: "'is'",
    ast.IsNot: "'is not'",
}


def _snippet(node: ast.AST) -> str:
    """Render a sub-expression for an error message, truncated.

    Safe to call on unvalidated nodes: ``ast.unparse`` is a formatter, it does not
    execute anything.
    """
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is total for parsed trees
        return "<expression>"
    if len(text) > _SNIPPET_LENGTH:
        return text[: _SNIPPET_LENGTH - 3] + "..."
    return text


def _describe_arity(minimum: int, maximum: int | None) -> str:
    if maximum is None:
        return f"at least {minimum} arguments"
    if maximum == minimum:
        return f"{minimum} argument(s)"
    return f"{minimum} to {maximum} arguments"


def _reject(node: ast.AST) -> DisallowedConstruct:
    name = _CONSTRUCT_NAMES.get(type(node), f"a {type(node).__name__} node")
    message = f"{name} is not allowed in a policy expression, in `{_snippet(node)}`"
    hint = _CONSTRUCT_HINTS.get(type(node))
    if hint is not None:
        message = f"{message}; {hint}"
    return DisallowedConstruct(message)


class _Validator:
    """Single-pass allowlist walk.

    Kept as a class only to carry the node counter; there is no state a caller can
    reach. Anything this walk does not explicitly permit is rejected, so a future
    Python release that adds a node type fails closed rather than silently widening
    the grammar.
    """

    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0

    def visit(self, node: ast.expr, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ExpressionTooComplex(
                f"expression uses more than {MAX_NODES} syntax nodes; split it into "
                f"several policy rules"
            )
        if depth > MAX_DEPTH:
            raise ExpressionTooComplex(
                f"expression nests deeper than {MAX_DEPTH} levels, in `{_snippet(node)}`"
            )

        if isinstance(node, ast.Constant):
            self._visit_constant(node)
        elif isinstance(node, ast.Name):
            self._visit_name(node)
        elif isinstance(node, ast.Attribute):
            self._visit_attribute(node, depth)
        elif isinstance(node, ast.Subscript):
            self._visit_subscript(node, depth)
        elif isinstance(node, ast.BoolOp):
            for operand in node.values:
                self.visit(operand, depth + 1)
        elif isinstance(node, ast.UnaryOp):
            self._visit_unary(node, depth)
        elif isinstance(node, ast.Compare):
            self._visit_compare(node, depth)
        elif isinstance(node, ast.Call):
            self._visit_call(node, depth)
        elif isinstance(node, ast.Tuple | ast.List | ast.Set):
            self._visit_display(node, depth)
        else:
            raise _reject(node)

    def _visit_constant(self, node: ast.Constant) -> None:
        if node.value is None or isinstance(node.value, bool | int | float | str):
            return
        raise DisallowedConstruct(
            f"a {type(node.value).__name__} literal is not allowed in a policy "
            f"expression; use a string, number, true, false or null"
        )

    def _visit_name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            # Unreachable in eval mode, asserted so that a future grammar change
            # cannot quietly introduce assignment.
            raise DisallowedConstruct(
                f"name {node.id!r} is used in a write position; policy expressions only read"
            )
        if node.id.startswith("__") or node.id.endswith("__"):
            raise DisallowedConstruct(
                f"the identifier {node.id!r} is not allowed; dunder names are reserved "
                f"and are never resolvable from a policy context"
            )

    def _visit_attribute(self, node: ast.Attribute, depth: int) -> None:
        if node.attr.startswith("__") or node.attr.endswith("__"):
            raise DisallowedConstruct(
                f"the attribute {node.attr!r} is not allowed; dunder attributes are "
                f"how expression sandboxes are escaped"
            )
        self.visit(node.value, depth + 1)

    def _visit_subscript(self, node: ast.Subscript, depth: int) -> None:
        if isinstance(node.slice, ast.Slice):
            raise _reject(node.slice)
        self.visit(node.value, depth + 1)
        self.visit(node.slice, depth + 1)

    def _visit_unary(self, node: ast.UnaryOp, depth: int) -> None:
        if not isinstance(node.op, ast.Not | ast.USub):
            raise DisallowedConstruct(
                f"the unary operator in `{_snippet(node)}` is not allowed; only 'not' "
                f"and unary minus are available"
            )
        self.visit(node.operand, depth + 1)

    def _visit_compare(self, node: ast.Compare, depth: int) -> None:
        for op in node.ops:
            if isinstance(op, ast.Is | ast.IsNot):
                raise DisallowedConstruct(
                    f"{_COMPARE_OP_NAMES[type(op)]} is not allowed in a policy "
                    f"expression; identity is almost never what a rule means, use == "
                    f"or != instead"
                )
            if not isinstance(
                op, ast.Eq | ast.NotEq | ast.Lt | ast.LtE | ast.Gt | ast.GtE | ast.In | ast.NotIn
            ):
                raise DisallowedConstruct(
                    f"the comparison operator in `{_snippet(node)}` is not allowed"
                )
        self.visit(node.left, depth + 1)
        for comparator in node.comparators:
            self.visit(comparator, depth + 1)

    def _visit_call(self, node: ast.Call, depth: int) -> None:
        if not isinstance(node.func, ast.Name):
            raise DisallowedConstruct(
                f"only the built-in policy helpers may be called, not "
                f"`{_snippet(node.func)}`; available helpers are "
                f"{', '.join(sorted(HELPER_NAMES))}"
            )
        name = node.func.id
        arity = _HELPER_ARITY.get(name)
        if arity is None:
            raise DisallowedConstruct(
                f"unknown function {name!r}; available helpers are "
                f"{', '.join(sorted(HELPER_NAMES))}"
            )
        if node.keywords:
            raise DisallowedConstruct(f"{name}() does not take keyword arguments")
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                raise _reject(argument)
        minimum, maximum = arity
        count = len(node.args)
        if count < minimum or (maximum is not None and count > maximum):
            raise DisallowedConstruct(
                f"{name}() takes {_describe_arity(minimum, maximum)}, got {count}"
            )
        for argument in node.args:
            self.visit(argument, depth + 1)

    def _visit_display(self, node: ast.Tuple | ast.List | ast.Set, depth: int) -> None:
        if len(node.elts) > MAX_ELEMENTS:
            raise ExpressionTooComplex(
                f"a list, tuple or set in a policy expression may hold at most "
                f"{MAX_ELEMENTS} elements"
            )
        for element in node.elts:
            if isinstance(element, ast.Starred):
                raise _reject(element)
            self.visit(element, depth + 1)


@dataclass(frozen=True, slots=True)
class CompiledExpression:
    """A validated expression, ready to evaluate against any number of contexts.

    Instances are immutable and the AST they hold is never mutated during evaluation,
    which is why :func:`compile_expression` can hand the same object to concurrent
    requests.
    """

    source: str
    tree: ast.Expression
    node_count: int

    def evaluate(self, context: Mapping[str, Any]) -> ExpressionOutcome:
        """Evaluate against ``context``. Never raises.

        Returns a failure outcome for anything that goes wrong, including a result
        that is not a boolean: a rule is a yes/no question, and an expression that
        answers with a string is a bug in the rule that must not be resolved by
        guessing at truthiness.
        """
        try:
            value = _eval(self.tree.body, context)
        except PolicyExpressionError as exc:
            return ExpressionOutcome.failure(str(exc))
        except RecursionError:
            return ExpressionOutcome.failure("expression nesting exhausted the evaluator's stack")
        except Exception as exc:  # noqa: BLE001 - totality is the whole contract
            # Only the exception's type is reported. A message from an arbitrary
            # exception can carry a value out of the context and into the audit log,
            # and no debugging convenience is worth that risk in this file.
            return ExpressionOutcome.failure(
                f"unexpected {type(exc).__name__} while evaluating the expression"
            )
        if not isinstance(value, bool):
            return ExpressionOutcome.failure(
                f"expression produced a {type(value).__name__}, but a policy rule must "
                f"produce true or false; add an explicit comparison"
            )
        return ExpressionOutcome.success(value)


@lru_cache(maxsize=_EXPRESSION_CACHE_SIZE)
def compile_expression(source: str) -> CompiledExpression:
    """Parse and validate ``source``, raising :class:`PolicyExpressionError` if invalid.

    Cached, because a golden path's rules are evaluated on every request and reparsing
    them is pure waste. The cache is keyed on the exact source text, so editing a rule
    produces a different key rather than a stale hit.
    """
    if not isinstance(source, str):  # pragma: no cover - defensive, schema enforces str
        raise ExpressionSyntaxError("a policy expression must be a string")
    stripped = source.strip()
    if not stripped:
        raise ExpressionSyntaxError(
            "empty policy expression; omit 'when' entirely to write an unconditional rule"
        )
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionTooComplex(
            f"policy expression is {len(source)} characters, the limit is {MAX_EXPRESSION_LENGTH}"
        )

    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        # SyntaxError carries the offending text; the text is the rule itself, written
        # by a platform engineer, so echoing the message is safe and useful.
        raise ExpressionSyntaxError(f"policy expression is not valid syntax: {exc.msg}") from exc
    except (RecursionError, MemoryError) as exc:
        # Deeply parenthesised input dies inside the parser, before any node budget
        # can see it. This is the only place that can catch it.
        raise ExpressionTooComplex("policy expression is nested too deeply to parse") from exc

    validator = _Validator()
    validator.visit(tree.body, depth=0)
    return CompiledExpression(source=source, tree=tree, node_count=validator.nodes)


def validate_expression(source: str) -> str | None:
    """Return ``None`` if ``source`` is a legal policy expression, else the problem.

    Used by the catalog loader and by ``bailment lint`` so that a malformed rule is
    caught when a golden path is written, not when an agent is waiting on a request.
    """
    try:
        compile_expression(source)
    except PolicyExpressionError as exc:
        return str(exc)
    return None


def evaluate_expression(source: str, context: Mapping[str, Any]) -> ExpressionOutcome:
    """Compile and evaluate in one call. Never raises."""
    try:
        compiled = compile_expression(source)
    except PolicyExpressionError as exc:
        return ExpressionOutcome.failure(str(exc))
    return compiled.evaluate(context)


# --- Evaluation -------------------------------------------------------------------


def _eval(node: ast.expr, ctx: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _eval_name(node, ctx)
    if isinstance(node, ast.Attribute):
        return _eval_attribute(node, ctx)
    if isinstance(node, ast.Subscript):
        return _eval_subscript(node, ctx)
    if isinstance(node, ast.BoolOp):
        return _eval_boolop(node, ctx)
    if isinstance(node, ast.UnaryOp):
        return _eval_unaryop(node, ctx)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, ctx)
    if isinstance(node, ast.Call):
        return _eval_call(node, ctx)
    if isinstance(node, ast.Tuple):
        return tuple(_eval(element, ctx) for element in node.elts)
    if isinstance(node, ast.List):
        return [_eval(element, ctx) for element in node.elts]
    if isinstance(node, ast.Set):
        return _eval_set(node, ctx)
    # Unreachable: the validator ran first. Kept because "unreachable" and "unreached"
    # are different claims, and this one fails closed.
    raise ExpressionRuntimeError(
        f"internal error: unvalidated {type(node).__name__} node reached the evaluator"
    )


def _eval_name(node: ast.Name, ctx: Mapping[str, Any]) -> Any:
    if node.id in _LITERAL_NAMES:
        return _LITERAL_NAMES[node.id]
    try:
        return ctx[node.id]
    except KeyError:
        raise ExpressionRuntimeError(
            f"unknown name {node.id!r}; the policy context provides {_available(ctx)}"
        ) from None


def _eval_attribute(node: ast.Attribute, ctx: Mapping[str, Any]) -> Any:
    target = _eval(node.value, ctx)
    if not isinstance(target, Mapping):
        raise ExpressionRuntimeError(
            f"cannot read {node.attr!r} from a {type(target).__name__} in "
            f"`{_snippet(node)}`; dotted access only works on structured values such "
            f"as 'input'"
        )
    try:
        return target[node.attr]
    except KeyError:
        raise ExpressionRuntimeError(
            f"`{_snippet(node.value)}` has no key {node.attr!r}; it provides {_available(target)}"
        ) from None


def _eval_subscript(node: ast.Subscript, ctx: Mapping[str, Any]) -> Any:
    target = _eval(node.value, ctx)
    key = _eval(node.slice, ctx)
    if isinstance(target, Mapping):
        try:
            return target[key]
        except (KeyError, TypeError):
            raise ExpressionRuntimeError(
                f"`{_snippet(node.value)}` has no such key; it provides {_available(target)}"
            ) from None
    if isinstance(target, list | tuple | str):
        if not isinstance(key, int) or isinstance(key, bool):
            raise ExpressionRuntimeError(
                f"a {type(target).__name__} must be indexed with a whole number, got a "
                f"{type(key).__name__} in `{_snippet(node)}`"
            )
        try:
            return target[key]
        except IndexError:
            raise ExpressionRuntimeError(
                f"index out of range in `{_snippet(node)}`; the value holds "
                f"{len(target)} element(s)"
            ) from None
    raise ExpressionRuntimeError(
        f"a {type(target).__name__} cannot be indexed, in `{_snippet(node)}`"
    )


def _eval_boolop(node: ast.BoolOp, ctx: Mapping[str, Any]) -> bool:
    is_and = isinstance(node.op, ast.And)
    result = is_and
    for operand in node.values:
        value = _require_bool(_eval(operand, ctx), operand, "and/or")
        if is_and and not value:
            return False
        if not is_and and value:
            return True
        result = value
    return result


def _eval_unaryop(node: ast.UnaryOp, ctx: Mapping[str, Any]) -> Any:
    value = _eval(node.operand, ctx)
    if isinstance(node.op, ast.Not):
        return not _require_bool(value, node.operand, "not")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ExpressionRuntimeError(
            f"unary minus needs a number, got a {type(value).__name__} in `{_snippet(node)}`"
        )
    return -value


def _eval_compare(node: ast.Compare, ctx: Mapping[str, Any]) -> bool:
    left = _eval(node.left, ctx)
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        right = _eval(comparator, ctx)
        if not _compare(op, left, right, node):
            return False
        # Chained comparisons reuse the middle operand, exactly as Python does.
        left = right
    return True


def _compare(op: ast.cmpop, left: Any, right: Any, node: ast.Compare) -> bool:
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.In):
        return _contains(right, left, node)
    if isinstance(op, ast.NotIn):
        return not _contains(right, left, node)
    try:
        if isinstance(op, ast.Lt):
            return bool(left < right)
        if isinstance(op, ast.LtE):
            return bool(left <= right)
        if isinstance(op, ast.Gt):
            return bool(left > right)
        if isinstance(op, ast.GtE):
            return bool(left >= right)
    except TypeError:
        raise ExpressionRuntimeError(
            f"cannot order a {type(left).__name__} against a {type(right).__name__} in "
            f"`{_snippet(node)}`"
        ) from None
    raise ExpressionRuntimeError(  # pragma: no cover - validator rejects the rest
        f"internal error: unvalidated comparison operator {type(op).__name__}"
    )


def _contains(container: Any, needle: Any, node: ast.Compare) -> bool:
    if isinstance(container, Mapping):
        # Key membership, which is what makes the
        # `"env" in input and input.env == "prod"` idiom work.
        try:
            return needle in container
        except TypeError:
            raise ExpressionRuntimeError(
                f"a {type(needle).__name__} cannot be a key, in `{_snippet(node)}`"
            ) from None
    if isinstance(container, str):
        if not isinstance(needle, str):
            raise ExpressionRuntimeError(
                f"'in' on a string needs a string on the left, got a "
                f"{type(needle).__name__} in `{_snippet(node)}`"
            )
        return needle in container
    if isinstance(container, list | tuple | set | frozenset):
        # Linear scan rather than hashed lookup so an unhashable left operand is a
        # false result instead of a TypeError.
        return any(needle == item for item in container)
    raise ExpressionRuntimeError(
        f"'in' needs a list, tuple, set, string or structured value on the right, got "
        f"a {type(container).__name__} in `{_snippet(node)}`"
    )


def _eval_set(node: ast.Set, ctx: Mapping[str, Any]) -> frozenset[Any]:
    values = [_eval(element, ctx) for element in node.elts]
    try:
        return frozenset(values)
    except TypeError:
        raise ExpressionRuntimeError(
            f"a set may only hold simple values, in `{_snippet(node)}`; use a list if "
            f"the elements are structured"
        ) from None


def _eval_call(node: ast.Call, ctx: Mapping[str, Any]) -> Any:
    # The validator guarantees both of these, but the check is re-done rather than
    # asserted: assertions vanish under -O, and this one stands between the expression
    # language and arbitrary callables.
    func = node.func
    if not isinstance(func, ast.Name) or func.id not in _HELPERS:
        raise ExpressionRuntimeError(
            "internal error: a call to something other than a policy helper reached the evaluator"
        )
    helper = _HELPERS[func.id]
    arguments = [_eval(argument, ctx) for argument in node.args]
    return helper(*arguments)


def _require_bool(value: Any, node: ast.expr, operator: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ExpressionRuntimeError(
        f"'{operator}' needs true or false, got a {type(value).__name__} in "
        f"`{_snippet(node)}`; policy expressions do not use truthiness, write the "
        f"comparison out"
    )


def _available(mapping: Mapping[str, Any]) -> str:
    """List a mapping's keys for an error message. Keys only -- never values."""
    keys = sorted(str(key) for key in mapping)
    if not keys:
        return "no keys"
    shown = keys[:12]
    text = ", ".join(repr(key) for key in shown)
    if len(keys) > len(shown):
        text = f"{text}, ... ({len(keys)} total)"
    return text


# --- Helper functions -------------------------------------------------------------
#
# Every one of these is pure, total on its declared types and loud on everything else.
# Nothing here touches the filesystem, the network, the clock or any object it was not
# handed.


def _helper_len(value: Any) -> int:
    if isinstance(value, str | list | tuple | set | frozenset | Mapping):
        return len(value)
    raise ExpressionRuntimeError(
        f"len() needs a string or a collection, got a {type(value).__name__}"
    )


def _helper_lower(value: Any) -> str:
    return _as_str(value, "lower").lower()


def _helper_upper(value: Any) -> str:
    return _as_str(value, "upper").upper()


def _helper_startswith(value: Any, prefix: Any) -> bool:
    return _as_str(value, "startswith").startswith(_as_str_tuple(prefix, "startswith"))


def _helper_endswith(value: Any, suffix: Any) -> bool:
    return _as_str(value, "endswith").endswith(_as_str_tuple(suffix, "endswith"))


def _helper_matches(value: Any, pattern: Any) -> bool:
    subject = _as_str(value, "matches")
    if not isinstance(pattern, str):
        raise ExpressionRuntimeError(
            f"matches() needs a regular expression string as its second argument, got "
            f"a {type(pattern).__name__}"
        )
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ExpressionRuntimeError(
            f"matches() pattern is longer than {MAX_PATTERN_LENGTH} characters"
        )
    if len(subject) > MAX_MATCH_SUBJECT_LENGTH:
        raise ExpressionRuntimeError(
            f"matches() refuses a subject longer than {MAX_MATCH_SUBJECT_LENGTH} characters"
        )
    return _compiled_pattern(pattern).fullmatch(subject) is not None


def _helper_any_of(value: Any, *options: Any) -> bool:
    """True when ``value`` equals any option.

    Accepts both ``any_of(env, "dev", "staging")`` and ``any_of(env, allowed_list)``;
    the second form is what makes it useful against a list that came from the context.
    """
    if len(options) == 1 and isinstance(options[0], list | tuple | set | frozenset):
        candidates: tuple[Any, ...] = tuple(options[0])
    else:
        candidates = options
    return any(value == candidate for candidate in candidates)


def _as_str(value: Any, helper: str) -> str:
    if isinstance(value, str):
        return value
    raise ExpressionRuntimeError(f"{helper}() needs a string, got a {type(value).__name__}")


def _as_str_tuple(value: Any, helper: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple | set | frozenset):
        items = tuple(value)
        if all(isinstance(item, str) for item in items):
            return tuple(str(item) for item in items)
    raise ExpressionRuntimeError(
        f"{helper}() needs a string or a collection of strings as its second argument, "
        f"got a {type(value).__name__}"
    )


@lru_cache(maxsize=_PATTERN_CACHE_SIZE)
def _compiled_pattern(pattern: str) -> re.Pattern[str]:
    """Compile and cache. A rule's pattern is fixed, so it is compiled once per process.

    Note the limit of this defence: a pathological pattern can still backtrack for a
    long time on an adversarial subject. Patterns come from the golden path YAML, which
    is the same trust level as the rest of the policy, so the exposure is a mistake by a
    platform engineer rather than an escalation by a requester. The subject length cap
    in :func:`_helper_matches` bounds how bad that mistake can get.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ExpressionRuntimeError(
            f"matches() pattern is not a valid regular expression: {exc.msg}"
        ) from exc


_HELPERS: Final[dict[str, Callable[..., Any]]] = {
    "len": _helper_len,
    "lower": _helper_lower,
    "upper": _helper_upper,
    "startswith": _helper_startswith,
    "endswith": _helper_endswith,
    "matches": _helper_matches,
    "any_of": _helper_any_of,
}

# The two tables must agree or a rule could pass validation and then fail to resolve.
assert set(_HELPERS) == set(_HELPER_ARITY)
