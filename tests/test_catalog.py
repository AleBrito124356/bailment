"""Loading golden paths off a disk, and the schema they have to satisfy.

This is the only file in the suite that parses YAML. Everything else builds
:class:`GoldenPath` objects directly, so the checks here are about what a platform team
actually experiences: twenty-five files, one of them wrong, and an error message that has
to name which one.

The shipped catalog is loaded too. It is the catalog every evaluator gets on their first
run, and a golden path that ships broken is worse than one that fails in CI.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from bailment.catalog import loader as loader_module
from bailment.catalog.loader import (
    Catalog,
    CatalogError,
    UnknownGoldenPath,
    aload_catalog,
    load_catalog,
)
from bailment.catalog.schema import (
    Duration,
    GoldenPath,
    LeasePolicy,
    format_duration,
    parse_duration,
)

SHIPPED_CATALOG = Path(loader_module.__file__).resolve().parent / "paths"

VALID = {
    "id": "widget",
    "name": "A widget",
    "description": "Something you can have for a while.",
    "provider": "memory",
    "inputs": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name"],
        "properties": {"name": {"type": "string", "maxLength": 40}},
    },
    "lease": {"default_ttl": "1h", "max_ttl": "4h", "warn_before": "10m"},
    "policy": [{"effect": "allow", "reason": "help yourself"}],
    "outputs": [{"name": "WIDGET_URL", "secret": True}],
}


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "paths"
    directory.mkdir()
    return directory


def write(directory: Path, name: str, document: object) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def test_a_valid_directory_loads(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", VALID)
    write(catalog_dir, "gadget.yml", {**VALID, "id": "gadget", "enabled": False})

    catalog = load_catalog(catalog_dir)

    assert len(catalog) == 2
    assert catalog.ids() == ("gadget", "widget")  # sorted, not filesystem order
    assert [path.id for path in catalog.enabled()] == ["widget"]
    assert catalog.get("widget").provider == "memory"
    assert catalog.source_of("widget").name == "widget.yaml"
    assert catalog.directory == catalog_dir
    assert "widget" in catalog
    assert "nope" not in catalog


def test_the_shipped_catalog_is_valid() -> None:
    """The four paths in the package are what an evaluator meets first."""
    catalog = load_catalog(SHIPPED_CATALOG)
    assert set(catalog.ids()) == {"dns-record", "postgres", "redis", "sandbox"}
    assert "memory" in catalog.providers()
    sandbox = catalog.get("sandbox")
    assert sandbox.provider == "memory"
    # The sandbox path is the zero-credential demo, so it must not be gated behind a
    # human: an evaluator with no cloud account has nobody to ask.
    assert [rule.effect for rule in sandbox.policy] == ["allow"]


async def test_aload_catalog_matches_the_synchronous_loader(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", VALID)
    assert (await aload_catalog(catalog_dir)).ids() == load_catalog(catalog_dir).ids()


# --------------------------------------------------------------------------------------
# Refusals, all of which have to name the file
# --------------------------------------------------------------------------------------


def test_a_last_rule_with_a_when_is_rejected(catalog_dir: Path) -> None:
    """Every request must get a decision, so the chain has to end unconditionally."""
    path = write(
        catalog_dir,
        "widget.yaml",
        {**VALID, "policy": [{"when": 'env == "dev"', "effect": "allow", "reason": "ok"}]},
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    message = str(caught.value)
    assert str(path) in message
    assert "last policy rule must be unconditional" in message


def test_an_earlier_unconditional_rule_is_rejected(catalog_dir: Path) -> None:
    """It would make every rule after it dead code."""
    write(
        catalog_dir,
        "widget.yaml",
        {
            **VALID,
            "policy": [
                {"effect": "allow", "reason": "first"},
                {"when": 'env == "prod"', "effect": "deny", "reason": "unreachable"},
                {"effect": "deny", "reason": "default"},
            ],
        },
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    assert "only the last policy rule may omit" in str(caught.value)


def test_duplicate_ids_across_files_are_rejected_and_both_files_are_named(
    catalog_dir: Path,
) -> None:
    """Whichever file loaded last would silently win, and that would depend on the disk."""
    write(catalog_dir, "a-widget.yaml", VALID)
    second = write(catalog_dir, "b-widget.yaml", {**VALID, "name": "The same id"})
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    message = str(caught.value)
    assert str(second) in message
    assert "a-widget.yaml" in message
    assert caught.value.field == "id"


def test_a_policy_expression_that_cannot_compile_is_refused_at_load_time(
    catalog_dir: Path,
) -> None:
    """A rule that cannot be evaluated denies every request that reaches it.

    Catching it here turns "this path silently refuses everybody" into a startup error
    with a filename and a rule index.
    """
    path = write(
        catalog_dir,
        "widget.yaml",
        {
            **VALID,
            "policy": [
                {"when": "input.__class__", "effect": "deny", "reason": "hostile"},
                {"effect": "allow", "reason": "default"},
            ],
        },
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    message = str(caught.value)
    assert str(path) in message
    assert "policy.0.when" in message
    assert caught.value.field == "policy"


def test_duplicate_output_names_are_rejected(catalog_dir: Path) -> None:
    """The binding payload is a dict, so a repeated name loses a credential quietly."""
    write(
        catalog_dir,
        "widget.yaml",
        {
            **VALID,
            "outputs": [{"name": "WIDGET_URL"}, {"name": "WIDGET_URL", "secret": False}],
        },
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    assert "duplicated: WIDGET_URL" in str(caught.value)


def test_a_required_input_that_is_never_declared_is_rejected(catalog_dir: Path) -> None:
    write(
        catalog_dir,
        "widget.yaml",
        {
            **VALID,
            "inputs": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "env"],
                "properties": {"name": {"type": "string"}},
            },
        },
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    assert "required but not in properties: env" in str(caught.value)


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        pytest.param({**VALID, "id": "X"}, "invalid golden path id", id="uppercase-id"),
        pytest.param({**VALID, "id": "a"}, "invalid golden path id", id="too-short-id"),
        pytest.param({**VALID, "name": ""}, "name", id="empty-name"),
        pytest.param({**VALID, "description": ""}, "description", id="empty-description"),
        pytest.param({**VALID, "policy": []}, "policy", id="no-policy"),
        pytest.param({**VALID, "colour": "blue"}, "colour", id="unknown-field"),
        pytest.param(
            {**VALID, "inputs": {"type": "string"}}, "type 'object'", id="inputs-not-an-object"
        ),
        pytest.param(
            {**VALID, "inputs": {"type": "object"}}, "properties", id="inputs-without-properties"
        ),
        pytest.param(
            {**VALID, "lease": {"default_ttl": "9h", "max_ttl": "4h"}},
            "exceeds max_ttl",
            id="default-over-max",
        ),
        pytest.param(
            {**VALID, "lease": {"default_ttl": "1h", "max_ttl": "4h", "warn_before": "2h"}},
            "born already expiring",
            id="warn-longer-than-ttl",
        ),
        pytest.param(
            {**VALID, "lease": {"default_ttl": "nonsense"}},
            "invalid duration",
            id="bad-duration",
        ),
        pytest.param(
            {**VALID, "outputs": [{"name": "lower_case"}]}, "outputs", id="bad-output-name"
        ),
    ],
)
def test_schema_violations_are_reported_against_the_file(
    catalog_dir: Path, document: dict[str, object], fragment: str
) -> None:
    path = write(catalog_dir, "widget.yaml", document)
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    message = str(caught.value)
    assert str(path) in message
    assert fragment in message
    assert caught.value.problems


def test_invalid_yaml_names_the_line(catalog_dir: Path) -> None:
    path = catalog_dir / "widget.yaml"
    path.write_text("id: widget\n  bad indentation: [\n", encoding="utf-8")
    with pytest.raises(CatalogError) as caught:
        load_catalog(catalog_dir)
    message = str(caught.value)
    assert str(path) in message
    assert "line" in message


def test_an_empty_file_is_refused(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", None)
    with pytest.raises(CatalogError, match="is empty"):
        load_catalog(catalog_dir)


def test_a_document_that_is_not_a_mapping_is_refused(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", ["a", "list"])
    with pytest.raises(CatalogError, match="mapping at the top level"):
        load_catalog(catalog_dir)


def test_yaml_is_parsed_safely(catalog_dir: Path) -> None:
    """A catalog file is configuration; configuration that can build Python objects is RCE."""
    (catalog_dir / "widget.yaml").write_text(
        "!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8"
    )
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_catalog(catalog_dir)


def test_a_missing_directory_says_which_variable_to_set(tmp_path: Path) -> None:
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path / "nowhere")
    assert "BAILMENT_CATALOG_DIR" in str(caught.value)


def test_a_file_where_a_directory_belongs_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "paths"
    target.write_text("", encoding="utf-8")
    with pytest.raises(CatalogError, match="not a directory"):
        load_catalog(target)


def test_an_empty_directory_is_refused_rather_than_serving_nothing(catalog_dir: Path) -> None:
    """An empty catalog is almost always an unmounted volume, not an intention."""
    (catalog_dir / "readme.md").write_text("not a golden path", encoding="utf-8")
    with pytest.raises(CatalogError, match="contains no"):
        load_catalog(catalog_dir)


# --------------------------------------------------------------------------------------
# Schema behaviour
# --------------------------------------------------------------------------------------


def test_additional_properties_is_forced_to_false(catalog_dir: Path) -> None:
    """Agents are enthusiastic. An open schema lets one smuggle unvalidated keys through.

    Three spellings, one answer: omitted, ``true``, and a sub-schema are all closed.
    """
    for index, additional in enumerate([None, True, {"type": "string"}]):
        inputs: dict[str, object] = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }
        if additional is not None:
            inputs["additionalProperties"] = additional
        write(
            catalog_dir,
            f"widget-{index}.yaml",
            {**VALID, "id": f"widget{index}", "inputs": inputs},
        )

    catalog = load_catalog(catalog_dir)
    for path in catalog:
        assert path.inputs["additionalProperties"] is False


def test_an_explicit_false_is_left_alone(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", VALID)
    assert load_catalog(catalog_dir).get("widget").inputs["additionalProperties"] is False


def test_the_tool_schema_adds_ttl_without_touching_the_original() -> None:
    """One definition, two renderings. The dashboard form and the agent's tool schema are
    generated from the same object, which is what makes them unable to drift."""
    path = GoldenPath.model_validate(VALID)
    schema = path.input_schema_with_lease()
    assert set(schema["properties"]) == {"name", "ttl"}
    assert schema["additionalProperties"] is False
    assert "ttl" not in path.inputs["properties"]
    assert "4h" in schema["properties"]["ttl"]["description"]
    assert "1h" in schema["properties"]["ttl"]["description"]


def test_the_mcp_tool_name_is_namespaced_and_hyphen_free() -> None:
    """Hyphens are legal in an id and illegal in most tool-name conventions."""
    path = GoldenPath.model_validate({**VALID, "id": "dns-record"})
    assert path.mcp_tool_name == "bailment_provision_dns_record"


def test_cost_derives_an_hourly_figure_from_a_monthly_one() -> None:
    path = GoldenPath.model_validate({**VALID, "cost": {"estimated_monthly_usd": 730.0}})
    assert path.cost.estimated_hourly_usd == pytest.approx(1.0)


def test_an_explicit_hourly_figure_wins() -> None:
    path = GoldenPath.model_validate(
        {**VALID, "cost": {"estimated_monthly_usd": 730.0, "estimated_hourly_usd": 0.5}}
    )
    assert path.cost.estimated_hourly_usd == 0.5


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("4h", 14400),
        ("30m", 1800),
        ("90s", 90),
        ("1h30m", 5400),
        ("1h30m15s", 5415),
        (" 2h ", 7200),
    ],
)
def test_durations_parse(raw: str, seconds: int) -> None:
    assert parse_duration(raw) == timedelta(seconds=seconds)


@pytest.mark.parametrize("raw", ["", "   ", "4", "4d", "PT4H", "-1h", "0s", "h", "4h30"])
def test_bad_durations_are_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(raw)


@pytest.mark.parametrize("raw", ["4h", "30m", "90s", "1h30m", "1h30m15s", "72h"])
def test_duration_formatting_round_trips_the_value_not_the_spelling(raw: str) -> None:
    """``format_duration`` normalises: ``90s`` comes back as ``1m30s``.

    The instant is what has to survive, not the text. Rendering is used in notices and
    tool results, where "1m30s" is the better of the two anyway.
    """
    rendered = format_duration(parse_duration(raw))
    assert parse_duration(rendered) == parse_duration(raw)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (90, "1m30s"), (3600, "1h"), (5415, "1h30m15s"), (259200, "72h")],
)
def test_duration_rendering(seconds: int, expected: str) -> None:
    assert format_duration(timedelta(seconds=seconds)) == expected


def test_a_duration_exposes_its_delta() -> None:
    duration = Duration("45m")
    assert duration.delta == timedelta(minutes=45)
    assert str(duration) == "45m"


def test_lease_policy_defaults_are_the_documented_ones() -> None:
    policy = LeasePolicy()
    assert str(policy.default_ttl) == "4h"
    assert str(policy.max_ttl) == "72h"
    assert str(policy.warn_before) == "30m"
    assert policy.renewable is True
    assert policy.max_renewals == 3


# --------------------------------------------------------------------------------------
# The Catalog object
# --------------------------------------------------------------------------------------


def test_an_unknown_id_suggests_the_one_that_was_meant(catalog_dir: Path) -> None:
    """The suggestion is for agents as much as humans: a model that calls ``postgress``
    can fix itself in one turn instead of guessing."""
    write(catalog_dir, "widget.yaml", VALID)
    catalog = load_catalog(catalog_dir)
    with pytest.raises(UnknownGoldenPath) as caught:
        catalog.get("widgets")
    message = str(caught.value)
    assert "Did you mean widget?" in message
    assert "Available: widget" in message


def test_find_returns_none_rather_than_raising(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", VALID)
    catalog = load_catalog(catalog_dir)
    assert catalog.find("widget") is not None
    assert catalog.find("nope") is None


def test_source_of_an_unknown_path_raises(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", VALID)
    with pytest.raises(UnknownGoldenPath):
        load_catalog(catalog_dir).source_of("nope")


def test_a_catalog_groups_by_provider(catalog_dir: Path) -> None:
    """The reconciler walks providers, not paths."""
    write(catalog_dir, "widget.yaml", VALID)
    write(catalog_dir, "gadget.yaml", {**VALID, "id": "gadget", "provider": "neon"})
    write(catalog_dir, "doodah.yaml", {**VALID, "id": "doodah", "provider": "neon"})

    catalog = load_catalog(catalog_dir)
    assert catalog.providers() == ("memory", "neon")
    assert [path.id for path in catalog.by_provider("neon")] == ["doodah", "gadget"]
    assert catalog.by_provider("nobody") == ()


def test_a_catalog_is_iterable_and_sized(catalog_dir: Path) -> None:
    write(catalog_dir, "widget.yaml", VALID)
    catalog = load_catalog(catalog_dir)
    assert len(catalog) == 1
    assert [path.id for path in catalog] == ["widget"]
    assert "1 paths" in repr(catalog)


def test_a_catalog_cannot_be_mutated_through_the_mapping_it_was_built_from() -> None:
    """Every surface reads it concurrently; one that could be edited underneath them could
    hand an agent a tool whose definition changed mid-request."""
    path = GoldenPath.model_validate(VALID)
    source = {"widget": path}
    catalog = Catalog(paths=source, sources={"widget": Path("widget.yaml")}, directory=Path("."))
    source["other"] = path
    assert catalog.ids() == ("widget",)
