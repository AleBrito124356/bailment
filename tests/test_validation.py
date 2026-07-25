"""Request input validation, which is the only place a golden path's schema is enforced.

The tool schema an MCP client validates against is advisory -- a client may implement as
much or as little of JSON Schema as it feels like -- and a dashboard form is a suggestion.
So everything downstream, including the policy engine's ``input.*`` names and the
providers' ``coerce_*`` helpers, is relying on this module and nothing else.

Two properties carry the weight:

**An unrecognised keyword is a refusal, not a shrug.** That is the opposite of what a
partial validator normally does, and it is the whole reason a partial validator is safe
here. A validator that silently ignored ``oneOf`` would accept requests the catalog author
believed were constrained, and the person who wrote the constraint would never find out.

**``maxLength`` is checked before ``pattern``.** Regexes come from catalog files and are
applied to strings supplied by agents, so the length ceiling has to bound what a
pathological pattern can be run against. A string property with no ``maxLength`` at all
gets one imposed for the same reason.
"""

from __future__ import annotations

from typing import Any

import pytest

from bailment.engine.validation import (
    UNBOUNDED_STRING_LIMIT,
    InputValidationError,
    SchemaError,
    validate_inputs,
)


def obj(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": dict(properties)}


# --------------------------------------------------------------------------------------
# Closed schemas
# --------------------------------------------------------------------------------------


def test_an_unknown_key_is_refused_with_a_suggestion() -> None:
    """Agents are enthusiastic. An open schema lets one smuggle unvalidated keys straight
    through to a provider, and a model told the name it meant fixes itself in one turn."""
    schema = obj(name={"type": "string"}, env={"type": "string"})
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {"name": "x", "nam": "y"})
    problem = caught.value.problems[0]
    assert "is not an accepted input" in problem
    assert "Did you mean name?" in problem
    assert "Accepted inputs are: env, name" in problem


def test_an_open_schema_lets_extra_keys_through_unchanged() -> None:
    """The catalog closes every schema before it is used, so this branch is only reachable
    from a hand-built one -- but it must not silently drop the value."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    assert validate_inputs(schema, {"name": "x", "extra": 1}) == {"name": "x", "extra": 1}


def test_a_sub_schema_for_additional_properties_is_applied() -> None:
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": {"type": "string", "maxLength": 5},
    }
    assert validate_inputs(schema, {"anything": "ok"}) == {"anything": "ok"}
    with pytest.raises(InputValidationError, match="maximum is 5"):
        validate_inputs(schema, {"anything": "far too long"})


# --------------------------------------------------------------------------------------
# Unsupported keywords
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keyword",
    ["oneOf", "anyOf", "allOf", "not", "if", "$ref", "patternProperties", "dependentRequired"],
)
def test_a_keyword_this_module_cannot_enforce_refuses_the_request(keyword: str) -> None:
    """Loudly, with the keyword named, instead of becoming an unvalidated field that
    reaches a provider."""
    schema = obj(name={"type": "string", keyword: [{"type": "string"}]})
    with pytest.raises(SchemaError) as caught:
        validate_inputs(schema, {"name": "x"})
    assert keyword in str(caught.value)
    assert "does not enforce" in str(caught.value)
    assert "Supported keywords are" in str(caught.value)


@pytest.mark.parametrize("keyword", ["oneOf", "allOf", "$ref", "patternProperties"])
def test_an_unsupported_keyword_at_the_root_is_not_currently_refused(keyword: str) -> None:
    """**A known gap, pinned here rather than asserted as correct.**

    :func:`validate_inputs` calls :func:`_validate_object` directly for the root schema,
    and :func:`_check_keywords` runs inside :func:`_validate_value` -- which the root
    never passes through. So a golden path whose *top-level* ``inputs`` block declares
    ``oneOf`` is accepted and silently unenforced, which is exactly the outcome the
    module's docstring says must not happen. Every nested position is checked correctly.

    The fix is a ``_check_keywords(schema, where)`` call in ``validate_inputs`` before it
    delegates. This test asserts today's behaviour so that the day somebody makes that
    change, this file tells them the gap is closed rather than quietly agreeing.
    """
    schema = {**obj(name={"type": "string"}), keyword: [{"type": "string"}]}
    assert validate_inputs(schema, {"name": "x"}) == {"name": "x"}


@pytest.mark.parametrize(
    "keyword", ["title", "description", "examples", "default", "format", "$comment", "deprecated"]
)
def test_annotation_keywords_are_ignored_rather_than_refused(keyword: str) -> None:
    """``format`` is here because JSON Schema defines it as an annotation unless a
    validator opts in, and asserting on it would make this stricter than the tool schema
    the agent was shown."""
    schema = {**obj(name={"type": "string", keyword: "anything"})}
    assert validate_inputs(schema, {"name": "x"}) == {"name": "x"}


def test_a_nested_unsupported_keyword_is_caught_too() -> None:
    schema = obj(tags={"type": "array", "items": {"type": "string", "contentEncoding": "b64"}})
    with pytest.raises(SchemaError, match="contentEncoding"):
        validate_inputs(schema, {"tags": ["a"]})


# --------------------------------------------------------------------------------------
# Strings
# --------------------------------------------------------------------------------------


def test_length_is_checked_before_the_pattern() -> None:
    """A pathological pattern can then only ever be run against a bounded input.

    The assertion that matters is that exactly one problem is reported: if both ran, the
    regex would have been handed the over-long string first.
    """
    schema = obj(name={"type": "string", "maxLength": 4, "pattern": "^(a+)+$"})
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {"name": "aaaaaaaaaaaaaaaaaaaa!"})
    assert caught.value.problems == ("input.name: is 21 characters, maximum is 4",)


def test_a_string_with_no_declared_ceiling_gets_one_anyway() -> None:
    """A provisioning input is a name, an environment or a hostname. There is no
    legitimate one megabyte of it."""
    schema = obj(note={"type": "string"})
    assert validate_inputs(schema, {"note": "x" * UNBOUNDED_STRING_LIMIT})
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {"note": "x" * (UNBOUNDED_STRING_LIMIT + 1)})
    assert str(UNBOUNDED_STRING_LIMIT) in caught.value.problems[0]


def test_string_constraints() -> None:
    schema = obj(name={"type": "string", "minLength": 3, "pattern": "^[a-z-]+$"})
    assert validate_inputs(schema, {"name": "orders"}) == {"name": "orders"}
    with pytest.raises(InputValidationError, match="minimum is 3"):
        validate_inputs(schema, {"name": "ab"})
    with pytest.raises(InputValidationError, match="does not match the required pattern"):
        validate_inputs(schema, {"name": "Orders"})


def test_patterns_are_unanchored_as_json_schema_says() -> None:
    """Every shipped path writes ``^...$`` anyway, so it gets anchoring either way."""
    assert validate_inputs(obj(name={"type": "string", "pattern": "orders"}), {"name": "my-orders"})


def test_an_invalid_pattern_in_a_catalog_file_is_the_operator_s_problem() -> None:
    with pytest.raises(SchemaError, match="not a valid regular expression"):
        validate_inputs(obj(name={"type": "string", "pattern": "[unclosed"}), {"name": "x"})


# --------------------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "value", "ok"),
    [
        ("string", "x", True),
        ("string", 1, False),
        ("integer", 5, True),
        ("integer", 5.5, False),
        # bool is a subclass of int in Python but not in JSON. Letting ``true`` satisfy
        # ``type: integer`` would send a boolean to a provider expecting a size.
        ("integer", True, False),
        ("number", 5, True),
        ("number", 5.5, True),
        ("number", True, False),
        ("boolean", True, True),
        ("boolean", 1, False),
        ("null", None, True),
        ("null", "", False),
        ("array", [], True),
        ("array", "x", False),
        ("object", {}, True),
        ("object", [], False),
    ],
)
def test_type_checks_follow_json_and_not_python(declared: str, value: Any, ok: bool) -> None:
    schema = obj(field={"type": declared})
    if ok:
        assert validate_inputs(schema, {"field": value}) == {"field": value}
    else:
        with pytest.raises(InputValidationError, match="expected"):
            validate_inputs(schema, {"field": value})


def test_a_union_of_types_is_accepted() -> None:
    schema = obj(field={"type": ["string", "null"]})
    assert validate_inputs(schema, {"field": None}) == {"field": None}
    assert validate_inputs(schema, {"field": "x"}) == {"field": "x"}
    with pytest.raises(InputValidationError, match="string or null"):
        validate_inputs(schema, {"field": 1})


def test_a_type_the_validator_does_not_know_is_a_schema_error() -> None:
    with pytest.raises(SchemaError, match="unknown"):
        validate_inputs(obj(field={"type": "date"}), {"field": "x"})


def test_a_type_mismatch_reports_one_problem_not_two() -> None:
    """Continuing into type-specific keywords would produce a second, confusing complaint
    about the same single mistake."""
    schema = obj(size={"type": "integer", "minimum": 1, "maximum": 10})
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {"size": "big"})
    assert len(caught.value.problems) == 1


# --------------------------------------------------------------------------------------
# Numbers, enums, arrays
# --------------------------------------------------------------------------------------


def test_numeric_bounds() -> None:
    schema = obj(
        size={
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "multipleOf": 2,
        }
    )
    assert validate_inputs(schema, {"size": 4}) == {"size": 4}
    assert "must be at least 1" in _problems(schema, {"size": 0})[0]
    assert "must be at most 10" in _problems(schema, {"size": 12})[0]
    assert "must be a multiple of 2" in _problems(schema, {"size": 3})[0]


def test_exclusive_numeric_bounds() -> None:
    schema = obj(ratio={"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1})
    assert validate_inputs(schema, {"ratio": 0.5}) == {"ratio": 0.5}
    assert "must be greater than 0" in _problems(schema, {"ratio": 0})[0]
    assert "must be less than 1" in _problems(schema, {"ratio": 1})[0]


def test_enums_and_consts() -> None:
    schema = obj(env={"enum": ["dev", "prod"]}, kind={"const": "branch"})
    assert validate_inputs(schema, {"env": "dev", "kind": "branch"})
    assert "is not one of" in _problems(schema, {"env": "staging", "kind": "branch"})[0]
    assert "must be 'branch'" in _problems(schema, {"env": "dev", "kind": "clone"})[0]


def test_arrays() -> None:
    schema = obj(
        tags={
            "type": "array",
            "items": {"type": "string", "maxLength": 4},
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
        }
    )
    assert validate_inputs(schema, {"tags": ["a", "b"]}) == {"tags": ["a", "b"]}
    assert "minimum is 1" in _problems(schema, {"tags": []})[0]
    assert "maximum is 3" in _problems(schema, {"tags": ["a", "b", "c", "d"]})[0]
    assert "duplicate item" in _problems(schema, {"tags": ["a", "a"]})[0]
    assert "input.tags[0]" in _problems(schema, {"tags": ["far too long"]})[0]


def test_unique_items_handles_unhashable_values() -> None:
    """JSON values include dicts and lists, which a set could not hold."""
    schema = obj(
        rows={"type": "array", "uniqueItems": True, "items": {"type": "object", "properties": {}}}
    )
    assert "duplicate item" in _problems(schema, {"rows": [{"a": 1}, {"a": 1}]})[0]


# --------------------------------------------------------------------------------------
# Required and defaults
# --------------------------------------------------------------------------------------


def test_a_missing_required_input_names_itself_and_its_description() -> None:
    schema = {
        **obj(env={"type": "string", "description": "Which environment's data.\nMore prose."}),
        "required": ["env"],
    }
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {})
    problem = caught.value.problems[0]
    assert "input.env: is required and was not supplied" in problem
    # Only the first line of the description; the rest is prose for a different audience.
    assert "Which environment's data." in problem
    assert "More prose" not in problem


def test_defaults_are_filled_in_and_persisted() -> None:
    """A rule reading ``input.simulate`` must see the same value the provider will, and a
    lease row that omits a defaulted field cannot be replayed."""
    schema = obj(
        simulate={"type": "string", "enum": ["ok", "fail"], "default": "ok"},
        name={"type": "string"},
    )
    assert validate_inputs(schema, {"name": "x"}) == {"name": "x", "simulate": "ok"}


def test_an_explicitly_supplied_value_always_beats_the_default() -> None:
    """Including when it is the same value the default holds."""
    schema = obj(simulate={"type": "string", "default": "ok"})
    assert validate_inputs(schema, {"simulate": "fail"}) == {"simulate": "fail"}
    assert validate_inputs(schema, {"simulate": "ok"}) == {"simulate": "ok"}


def test_a_default_that_does_not_satisfy_its_own_schema_is_reported() -> None:
    """An operator's mistake, and one that would otherwise reach a provider unchallenged."""
    schema = obj(size={"type": "integer", "minimum": 10, "default": 1})
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {})
    assert "<default>" in caught.value.problems[0]


# --------------------------------------------------------------------------------------
# Reporting and shape
# --------------------------------------------------------------------------------------


def test_every_problem_is_collected_before_raising() -> None:
    """The consumer is frequently a model that will re-issue the call; telling it about
    three mistakes at once costs it one turn instead of three."""
    schema = {
        **obj(
            name={"type": "string", "minLength": 5},
            size={"type": "integer", "maximum": 3},
        ),
        "required": ["env"],
    }
    schema["properties"]["env"] = {"type": "string"}
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, {"name": "ab", "size": 9, "nonsense": 1})
    assert len(caught.value.problems) == 4
    assert str(caught.value).startswith("invalid inputs: ")


def test_a_schema_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(SchemaError, match="must be a JSON Schema object"):
        validate_inputs({"type": "string"}, {})


def test_values_that_are_not_an_object_are_refused() -> None:
    with pytest.raises(InputValidationError, match="expected an object"):
        validate_inputs(obj(), ["not", "a", "mapping"])  # type: ignore[arg-type]


def test_a_schema_nested_deeper_than_the_ceiling_is_refused() -> None:
    """Also stops a self-referential document from recursing without bound."""
    schema: dict[str, Any] = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(12):
        schema = {"type": "object", "properties": {"nested": schema}}
    value: Any = {"leaf": "x"}
    for _ in range(12):
        value = {"nested": value}
    with pytest.raises(SchemaError, match="nests deeper"):
        validate_inputs(schema, value)


@pytest.mark.parametrize(
    ("schema", "value", "match"),
    [
        pytest.param(
            {"type": "object", "properties": []}, "x", "properties", id="properties-not-a-map"
        ),
        pytest.param(
            {"type": "object", "properties": {"a": "nope"}},
            "x",
            "must be an object",
            id="prop-not-a-map",
        ),
        pytest.param(
            {"type": "object", "properties": {}, "required": "env"},
            "x",
            "required",
            id="required-not-a-list",
        ),
        pytest.param(
            {"type": "object", "properties": {}, "required": [1]},
            "x",
            "must hold strings",
            id="required-not-strings",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"enum": "x"}}},
            "x",
            "enum must be an array",
            id="enum-not-a-list",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "string", "maxLength": "5"}}},
            "x",
            "maxLength must be an integer",
            id="maxlength-not-an-int",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "string", "minLength": "5"}}},
            "x",
            "minLength must be an integer",
            id="minlength-not-an-int",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "string", "pattern": 5}}},
            "x",
            "pattern must be a string",
            id="pattern-not-a-string",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "integer", "multipleOf": 0}}},
            3,
            "positive number",
            id="multipleof-zero",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "integer", "minimum": "1"}}},
            3,
            "minimum must be a number",
            id="minimum-not-a-number",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "array", "items": []}}},
            [],
            "items must be an object",
            id="items-not-a-map",
        ),
    ],
)
def test_a_malformed_schema_is_the_operator_s_problem_not_the_caller_s(
    schema: dict[str, Any], value: Any, match: str
) -> None:
    """Different exception, different audience, different status code: nothing the caller
    sends will fix any of these."""
    with pytest.raises(SchemaError, match=match):
        validate_inputs(schema, {"a": value})


def test_the_returned_copy_does_not_alias_the_request() -> None:
    supplied = {"tags": ["a"], "nested": {"k": "v"}}
    schema = obj(
        tags={"type": "array", "items": {"type": "string"}},
        nested={"type": "object", "properties": {"k": {"type": "string"}}},
    )
    result = validate_inputs(schema, supplied)
    result["tags"].append("b")
    assert supplied["tags"] == ["a"]


def _problems(schema: dict[str, Any], values: dict[str, Any]) -> list[str]:
    with pytest.raises(InputValidationError) as caught:
        validate_inputs(schema, values)
    return list(caught.value.problems)
