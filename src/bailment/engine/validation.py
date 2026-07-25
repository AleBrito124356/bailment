"""Request input validation against a golden path's JSON Schema.

A golden path's ``inputs`` block is used verbatim in three places: as the MCP tool's
input schema, as the definition the dashboard form renders from, and here. The first two
are advisory -- an MCP client may implement as much or as little of JSON Schema as it
feels like, and a form is a suggestion -- so this module is the only place where the
schema is actually enforced. Everything downstream, including the policy engine's
``input.*`` names and the provider's ``coerce_*`` helpers, assumes the values it sees got
past this function.

**Why a subset validator rather than a JSON Schema library.** bailment ships a small,
deliberate dependency set, and a full Draft 2020-12 implementation would be the largest
thing in it for a job that never sees ``$ref``, ``allOf`` or dynamic anchors. The trade
is only acceptable because of the next paragraph.

**Unrecognised keywords are a refusal, not a shrug.** If a catalog file uses a keyword
this module does not implement, validation raises :class:`SchemaError` and the request is
denied. That is the opposite of what a partial validator normally does, and it is the
whole reason a partial validator is safe here: a validator that silently ignores
``oneOf`` will happily accept a request the catalog author believed was constrained, and
the person who wrote the constraint will never find out. Refusing loudly means the gap
shows up the first time anybody exercises the path, with the keyword named, instead of
becoming an unvalidated field that reaches a provider.

**Order of checks within a property is not arbitrary.** ``maxLength`` is evaluated before
``pattern``. Regexes come from catalog files written by operators and are applied to
strings supplied by agents; checking the length ceiling first means a pathological
pattern can only ever be run against a bounded input. For the same reason a string
property that declares no ``maxLength`` at all gets :data:`UNBOUNDED_STRING_LIMIT`
imposed on it -- a provisioning input is a name, an environment or a hostname, and there
is no legitimate one megabyte of it.

Every problem in a request is collected before raising, rather than failing on the first
one. The consumer here is frequently a model that will re-issue the call; telling it
about three mistakes at once costs it one turn instead of three.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any, Final

__all__ = [
    "UNBOUNDED_STRING_LIMIT",
    "InputValidationError",
    "SchemaError",
    "validate_inputs",
]

#: Ceiling applied to a string property that declares no ``maxLength`` of its own.
#: Generous for anything a provisioning request legitimately carries, and small enough
#: that no regex in a catalog file can be made expensive by a caller.
UNBOUNDED_STRING_LIMIT: Final = 4096

#: Keywords that carry documentation or defaults and constrain nothing. Ignored on
#: purpose. ``format`` is here because JSON Schema defines it as an annotation unless a
#: validator opts into assertion behaviour, and quietly asserting on it would make this
#: module stricter than the tool schema an agent was shown.
_ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "format",
        "readOnly",
        "title",
        "writeOnly",
    }
)

#: Keywords this module enforces. Anything outside the union of this and the annotation
#: set is a :class:`SchemaError`; see the module docstring.
_SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "additionalProperties",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
)

_TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)

#: Deeper than this and the schema is doing something a provisioning input should not.
#: Also stops a self-referential document from recursing without bound.
_MAX_DEPTH: Final = 8


class SchemaError(Exception):
    """The golden path's schema itself is unusable.

    Distinct from :class:`InputValidationError` because the two have different audiences
    and different HTTP statuses: this one is the operator's problem and nothing the
    caller sends will fix it.
    """


class InputValidationError(ValueError):
    """The caller's inputs do not satisfy the schema.

    :attr:`problems` is kept structured as well as being formatted into the message so
    the API can return a list and the MCP tool can return one string.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        joined = "; ".join(self.problems)
        super().__init__(f"invalid inputs: {joined}")


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _type_matches(expected: str, value: Any) -> bool:  # noqa: ANN401 - JSON is Any
    match expected:
        case "string":
            return isinstance(value, str)
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            # bool is a subclass of int in Python but not in JSON. Letting ``true``
            # satisfy ``type: integer`` would send a boolean to a provider expecting a
            # size, which fails much further downstream than it should.
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "object":
            return isinstance(value, Mapping)
        case "array":
            return isinstance(value, list)
        case "null":
            return value is None
    return False


def _describe(value: Any) -> str:  # noqa: ANN401 - JSON is Any
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _check_keywords(schema: Mapping[str, Any], where: str) -> None:
    unknown = sorted(set(schema) - _SUPPORTED_KEYWORDS - _ANNOTATION_KEYWORDS)
    if unknown:
        raise SchemaError(
            f"golden path schema at {where} uses JSON Schema keyword(s) "
            f"{', '.join(repr(k) for k in unknown)}, which bailment does not enforce. "
            f"The request is refused rather than validated against a weaker schema than "
            f"the one the catalog declares. Supported keywords are: "
            f"{', '.join(sorted(_SUPPORTED_KEYWORDS))}."
        )


def _schema_at(schema: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = schema[key]
    if not isinstance(value, Mapping):
        raise SchemaError(
            f"golden path schema at {where}.{key} must be an object, found {_describe(value)}"
        )
    return value


def _validate_value(
    schema: Mapping[str, Any],
    value: Any,  # noqa: ANN401 - JSON is Any
    *,
    where: str,
    problems: list[str],
    depth: int,
) -> Any:  # noqa: ANN401 - JSON is Any
    """Check one value, returning it with any nested defaults filled in.

    Appends to ``problems`` rather than raising, so one pass reports everything wrong.
    """
    if depth > _MAX_DEPTH:
        raise SchemaError(f"golden path schema nests deeper than {_MAX_DEPTH} levels at {where}")
    _check_keywords(schema, where)

    declared = schema.get("type")
    if declared is not None:
        expected = [declared] if isinstance(declared, str) else list(declared)
        for name in expected:
            if not isinstance(name, str) or name not in _TYPE_NAMES:
                raise SchemaError(f"golden path schema at {where}.type declares unknown {name!r}")
        if not any(_type_matches(name, value) for name in expected):
            problems.append(f"{where}: expected {' or '.join(expected)}, got {_describe(value)}")
            # Every remaining keyword is type-specific, so continuing would produce a
            # second, confusing complaint about the same single mistake.
            return value

    if "const" in schema and value != schema["const"]:
        problems.append(f"{where}: must be {schema['const']!r}")
        return value

    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list):
            raise SchemaError(f"golden path schema at {where}.enum must be an array")
        if value not in allowed:
            problems.append(
                f"{where}: {value!r} is not one of {', '.join(repr(a) for a in allowed)}"
            )
            return value

    if isinstance(value, str):
        _validate_string(schema, value, where=where, problems=problems)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        _validate_number(schema, value, where=where, problems=problems)
    elif isinstance(value, list):
        return _validate_array(schema, value, where=where, problems=problems, depth=depth)
    elif isinstance(value, Mapping):
        return _validate_object(schema, value, where=where, problems=problems, depth=depth)

    return value


def _validate_string(
    schema: Mapping[str, Any], value: str, *, where: str, problems: list[str]
) -> None:
    maximum = schema.get("maxLength")
    if maximum is None:
        if len(value) > UNBOUNDED_STRING_LIMIT:
            problems.append(
                f"{where}: is {len(value)} characters; the schema sets no maxLength so "
                f"bailment applies a {UNBOUNDED_STRING_LIMIT} character ceiling"
            )
            return
    else:
        if not isinstance(maximum, int) or isinstance(maximum, bool):
            raise SchemaError(f"golden path schema at {where}.maxLength must be an integer")
        if len(value) > maximum:
            problems.append(f"{where}: is {len(value)} characters, maximum is {maximum}")
            # Bail before the regex; see the module docstring on check ordering.
            return

    minimum = schema.get("minLength")
    if minimum is not None:
        if not isinstance(minimum, int) or isinstance(minimum, bool):
            raise SchemaError(f"golden path schema at {where}.minLength must be an integer")
        if len(value) < minimum:
            problems.append(f"{where}: is {len(value)} characters, minimum is {minimum}")

    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise SchemaError(f"golden path schema at {where}.pattern must be a string")
        try:
            compiled = _compile(pattern)
        except re.error as exc:
            raise SchemaError(
                f"golden path schema at {where}.pattern is not a valid regular expression: {exc}"
            ) from exc
        # search, not fullmatch: JSON Schema patterns are unanchored, and a catalog that
        # writes ^...$ (as every shipped path does) gets anchoring either way.
        if compiled.search(value) is None:
            problems.append(f"{where}: {value!r} does not match the required pattern {pattern}")


def _validate_number(
    schema: Mapping[str, Any], value: float, *, where: str, problems: list[str]
) -> None:
    def bound_of(keyword: str) -> float | None:
        raw = schema.get(keyword)
        if raw is None:
            return None
        if not isinstance(raw, int | float) or isinstance(raw, bool):
            raise SchemaError(f"golden path schema at {where}.{keyword} must be a number")
        return raw

    minimum = bound_of("minimum")
    if minimum is not None and value < minimum:
        problems.append(f"{where}: must be at least {minimum}, got {value}")
    maximum = bound_of("maximum")
    if maximum is not None and value > maximum:
        problems.append(f"{where}: must be at most {maximum}, got {value}")
    exclusive_minimum = bound_of("exclusiveMinimum")
    if exclusive_minimum is not None and value <= exclusive_minimum:
        problems.append(f"{where}: must be greater than {exclusive_minimum}, got {value}")
    exclusive_maximum = bound_of("exclusiveMaximum")
    if exclusive_maximum is not None and value >= exclusive_maximum:
        problems.append(f"{where}: must be less than {exclusive_maximum}, got {value}")

    step = schema.get("multipleOf")
    if step is not None:
        if not isinstance(step, int | float) or isinstance(step, bool) or step <= 0:
            raise SchemaError(f"golden path schema at {where}.multipleOf must be a positive number")
        if value % step != 0:
            problems.append(f"{where}: must be a multiple of {step}, got {value}")


def _validate_array(
    schema: Mapping[str, Any],
    value: list[Any],
    *,
    where: str,
    problems: list[str],
    depth: int,
) -> list[Any]:
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and not isinstance(minimum, bool) and len(value) < minimum:
        problems.append(f"{where}: has {len(value)} items, minimum is {minimum}")
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and not isinstance(maximum, bool) and len(value) > maximum:
        problems.append(f"{where}: has {len(value)} items, maximum is {maximum}")
        return value
    if schema.get("uniqueItems") is True:
        # Not a set: JSON values include dicts and lists, which are unhashable. n is
        # bounded by maxItems (or by the request body limit above this layer).
        seen: list[Any] = []
        for item in value:
            if item in seen:
                problems.append(f"{where}: contains duplicate item {item!r}")
                break
            seen.append(item)

    if "items" not in schema:
        return list(value)
    item_schema = _schema_at(schema, "items", where)
    return [
        _validate_value(
            item_schema, item, where=f"{where}[{index}]", problems=problems, depth=depth + 1
        )
        for index, item in enumerate(value)
    ]


def _validate_object(
    schema: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    where: str,
    problems: list[str],
    depth: int,
) -> dict[str, Any]:
    raw_properties = schema.get("properties", {})
    if not isinstance(raw_properties, Mapping):
        raise SchemaError(f"golden path schema at {where}.properties must be an object")
    properties: dict[str, Mapping[str, Any]] = {}
    for name, sub in raw_properties.items():
        if not isinstance(sub, Mapping):
            raise SchemaError(f"golden path schema at {where}.properties.{name} must be an object")
        properties[str(name)] = sub

    required = schema.get("required", [])
    if not isinstance(required, list):
        raise SchemaError(f"golden path schema at {where}.required must be an array")

    additional = schema.get("additionalProperties", True)

    for name in required:
        if not isinstance(name, str):
            raise SchemaError(f"golden path schema at {where}.required must hold strings")
        if name not in value:
            described = properties.get(name, {}).get("description", "")
            hint = f" ({described.strip().splitlines()[0]})" if described else ""
            problems.append(f"{where}.{name}: is required and was not supplied{hint}")

    result: dict[str, Any] = {}
    for name, item in value.items():
        key = str(name)
        sub = properties.get(key)
        if sub is not None:
            result[key] = _validate_value(
                sub, item, where=f"{where}.{key}", problems=problems, depth=depth + 1
            )
            continue
        if additional is False:
            close = difflib.get_close_matches(key, properties, n=2, cutoff=0.6)
            suggestion = f" Did you mean {' or '.join(close)}?" if close else ""
            known = ", ".join(sorted(properties)) or "<none>"
            problems.append(
                f"{where}.{key}: is not an accepted input.{suggestion} Accepted inputs are: {known}"
            )
            continue
        if isinstance(additional, Mapping):
            result[key] = _validate_value(
                additional, item, where=f"{where}.{key}", problems=problems, depth=depth + 1
            )
            continue
        result[key] = item

    # Defaults are applied after the supplied values so that a caller who sent the key
    # explicitly always wins, including when they sent the same value the default holds.
    for name, sub in properties.items():
        if name in result or "default" not in sub:
            continue
        default = sub["default"]
        result[name] = _validate_value(
            sub, default, where=f"{where}.{name}<default>", problems=problems, depth=depth + 1
        )

    return result


def validate_inputs(
    schema: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    where: str = "input",
) -> dict[str, Any]:
    """Validate ``values`` against ``schema``, returning a copy with defaults applied.

    Raises :class:`InputValidationError` listing every problem, or :class:`SchemaError`
    if the schema itself cannot be enforced.

    The returned dict is what gets persisted on the lease and what policy evaluates, so
    the defaults filled in here are the values the audit trail will show. That is
    deliberate: a rule reading ``input.simulate`` must see the same value the provider
    will, and a lease row that omits a defaulted field cannot be replayed.
    """
    if schema.get("type") != "object":
        raise SchemaError(
            f"golden path inputs must be a JSON Schema object; found type {schema.get('type')!r}"
        )
    if not isinstance(values, Mapping):
        raise InputValidationError([f"{where}: expected an object, got {_describe(values)}"])

    problems: list[str] = []
    result = _validate_object(schema, values, where=where, problems=problems, depth=0)
    if problems:
        raise InputValidationError(problems)
    return result
