"""Reading a directory of golden path YAML files into a validated catalog.

The whole point of this module is the error messages. A platform team's catalog grows to
twenty-five files, somebody adds one with ``efect: allow``, and pydantic reports
``policy.0.effect: Field required`` with no indication of which of the twenty-five files
it came from. Every error raised here names the file, the field path and, for YAML syntax
errors, the line -- because the person reading it is usually not the person who wrote the
file, and they are usually reading it in CI output at the end of the day.

Three checks live here rather than in :mod:`bailment.catalog.schema`, because they are
about a whole file or a whole directory rather than about one field:

* **Policy expressions compile.** A rule that cannot be evaluated denies every request
  that reaches it, so a misspelled context name is not a warning -- it is a golden path
  that says no to everybody until somebody reads the audit log carefully.

* **Duplicate ids across files.** Whichever file loaded last would silently win, and the
  golden path an agent gets would depend on filesystem ordering.
* **Duplicate output names within a path.** The binding payload is a dict, so a repeated
  name loses a credential quietly -- the lease goes active, one of the two values the
  consumer needs is simply absent, and the failure surfaces inside the agent's process.

Loading is synchronous. It happens at startup, before the event loop is serving anything,
and a blocking ``read_text`` there costs nothing. :func:`aload_catalog` exists for the one
case that is not startup: reloading the catalog from a running server without stalling the
loop while a network filesystem thinks about it.
"""

from __future__ import annotations

import asyncio
import difflib
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import yaml
from pydantic import ValidationError

from bailment.catalog.schema import GoldenPath

#: Extensions treated as catalog files. ``.yml`` is accepted because half the world writes
#: it and being right about the spelling is not worth an empty catalog.
CATALOG_SUFFIXES = (".yaml", ".yml")


class CatalogError(Exception):
    """A catalog file is missing, unreadable or invalid.

    :attr:`path` and :attr:`field` are kept as attributes as well as being formatted into
    the message, so the dashboard can render "this file, this field" without parsing
    prose back apart.
    """

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        field: str | None = None,
        problems: Sequence[str] = (),
    ) -> None:
        self.path = path
        self.field = field
        self.problems = tuple(problems)
        location = f"{path}" if path is not None else "catalog"
        if field:
            location = f"{location}: {field}"
        detail = "".join(f"\n  - {p}" for p in self.problems)
        super().__init__(f"{location}: {message}{detail}")


class UnknownGoldenPath(LookupError):
    """Asked for a golden path id this catalog does not have.

    Not a ``KeyError``: a KeyError's ``str()`` is its argument in quotes, so the helpful
    part of the message -- the close matches -- gets rendered as literal quoted text in
    half the places it surfaces.
    """


def _format_loc(loc: Sequence[Any]) -> str:
    """Render a pydantic error location as ``policy.0.effect``."""
    parts: list[str] = []
    for item in loc:
        parts.append(str(item))
    return ".".join(parts) or "<root>"


def _raise_validation_error(path: Path, error: ValidationError) -> NoReturn:
    problems: list[str] = []
    first_field: str | None = None
    for detail in error.errors():
        field = _format_loc(detail.get("loc", ()))
        if first_field is None:
            first_field = field
        message = str(detail.get("msg", "invalid"))
        # 'input' can be the whole document for a root-level error; only include it when
        # it is small enough to actually help.
        given = detail.get("input")
        rendered = repr(given)
        suffix = f" (got {rendered})" if len(rendered) <= 60 else ""
        problems.append(f"{field}: {message}{suffix}")
    raise CatalogError(
        "is not a valid golden path", path=path, field=first_field, problems=problems
    )


def _load_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"could not be read ({exc.strerror})", path=path) from exc
    try:
        # safe_load, never load: a catalog file is configuration, and configuration that
        # can construct arbitrary Python objects is a remote code execution primitive.
        document = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown position"
        raise CatalogError(f"is not valid YAML at {where}: {exc.problem}", path=path) from exc
    except yaml.YAMLError as exc:
        raise CatalogError(f"is not valid YAML: {exc}", path=path) from exc

    if document is None:
        raise CatalogError("is empty", path=path)
    if not isinstance(document, dict):
        raise CatalogError(
            f"must contain a mapping at the top level, found {type(document).__name__}", path=path
        )
    return document


def _check_policy_expressions(path: Path, golden_path: GoldenPath) -> None:
    """Compile every ``when`` now, so a typo cannot become a denial later.

    A rule that fails to evaluate denies the request -- see
    :mod:`bailment.policy.engine` for why it must -- which means a misspelled name in a
    catalog file is not a warning, it is a path that refuses everything the moment an
    agent touches it. Catching it here turns that into a startup error with a filename.

    The import is local because :mod:`bailment.policy.engine` imports
    :mod:`bailment.catalog.schema`, and a module-level import here would make the two
    packages import each other during interpreter start-up. Nothing about that cycle is
    load-bearing; it is just not worth the fragility for one call.
    """
    from bailment.policy.evaluator import validate_expression

    problems: list[str] = []
    for index, rule in enumerate(golden_path.policy):
        if rule.when is None:
            continue
        problem = validate_expression(rule.when)
        if problem is not None:
            problems.append(f"policy.{index}.when: {problem}")
    if problems:
        raise CatalogError(
            "contains a policy rule that is not a valid expression. A rule that cannot "
            "be evaluated denies every request, so this is refused at load time rather "
            "than discovered by whoever gets blocked first",
            path=path,
            field="policy",
            problems=problems,
        )


def _check_semantics(path: Path, golden_path: GoldenPath) -> None:
    """File-level checks a JSON Schema will not make for us."""
    counts = Counter(output.name for output in golden_path.outputs)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise CatalogError(
            "declares the same output name more than once, so one of the values would be "
            "lost when the binding payload is built",
            path=path,
            field="outputs",
            problems=[f"duplicated: {name}" for name in duplicates],
        )

    properties = golden_path.inputs.get("properties", {})
    required = golden_path.inputs.get("required", [])
    if isinstance(properties, dict) and isinstance(required, list):
        missing = [name for name in required if name not in properties]
        if missing:
            raise CatalogError(
                "marks inputs as required that it never declares, so every request will "
                "be rejected by schema validation",
                path=path,
                field="inputs.required",
                problems=[f"required but not in properties: {name}" for name in missing],
            )


def load_catalog(directory: Path | str) -> Catalog:
    """Load and validate every golden path in ``directory``.

    Files are read in sorted order so that a duplicate id always names the same pair of
    files, whatever the filesystem feels like doing today.
    """
    root = Path(directory)
    if not root.exists():
        raise CatalogError(
            "catalog directory does not exist; set BAILMENT_CATALOG_DIR or mount it",
            path=root,
        )
    if not root.is_dir():
        raise CatalogError("catalog path is not a directory", path=root)

    files = sorted(
        p for p in root.iterdir() if p.is_file() and p.suffix.lower() in CATALOG_SUFFIXES
    )
    if not files:
        raise CatalogError(
            f"contains no {' or '.join(CATALOG_SUFFIXES)} files. An empty catalog means "
            f"nothing can be provisioned, which is almost always a wrong path or an "
            f"unmounted volume rather than an intention",
            path=root,
        )

    paths: dict[str, GoldenPath] = {}
    sources: dict[str, Path] = {}
    for file in files:
        document = _load_document(file)
        try:
            golden_path = GoldenPath.model_validate(document)
        except ValidationError as exc:
            _raise_validation_error(file, exc)
        _check_semantics(file, golden_path)
        _check_policy_expressions(file, golden_path)

        existing = sources.get(golden_path.id)
        if existing is not None:
            raise CatalogError(
                f"declares golden path id {golden_path.id!r}, which {existing.name} already "
                f"declares. Ids appear in MCP tool names and URLs, so one of them would "
                f"silently shadow the other",
                path=file,
                field="id",
            )
        paths[golden_path.id] = golden_path
        sources[golden_path.id] = file

    return Catalog(paths=paths, sources=sources, directory=root)


async def aload_catalog(directory: Path | str) -> Catalog:
    """Load off the event loop. For reloading from a running server."""
    return await asyncio.to_thread(load_catalog, directory)


def load_default_catalog() -> Catalog:
    """Load from ``BAILMENT_CATALOG_DIR``, or the four paths shipped with the package."""
    from bailment.config import get_settings

    return load_catalog(get_settings().catalog_dir)


class Catalog:
    """An immutable, validated set of golden paths.

    Immutable because the API, the MCP server and the dashboard all read it concurrently
    and a catalog that can be edited underneath them is a catalog that can hand an agent
    a tool whose definition changed between the schema it validated against and the
    provider it reached. Reloading builds a new instance and swaps it in whole.
    """

    __slots__ = ("_directory", "_paths", "_sources")

    def __init__(
        self,
        paths: Mapping[str, GoldenPath],
        sources: Mapping[str, Path],
        directory: Path,
    ) -> None:
        self._paths: dict[str, GoldenPath] = dict(sorted(paths.items()))
        self._sources: dict[str, Path] = dict(sources)
        self._directory = directory

    @property
    def directory(self) -> Path:
        """Where this catalog was read from. Shown in the dashboard's footer."""
        return self._directory

    def get(self, path_id: str) -> GoldenPath:
        """Look up one golden path, or raise :class:`UnknownGoldenPath`.

        The suggestion in the error is for agents as much as humans: a model that calls
        ``postgress`` gets told the name it meant and can fix itself in one turn instead
        of guessing.
        """
        try:
            return self._paths[path_id]
        except KeyError:
            close = difflib.get_close_matches(path_id, self._paths, n=3, cutoff=0.6)
            hint = f" Did you mean {', '.join(close)}?" if close else ""
            raise UnknownGoldenPath(
                f"no golden path with id {path_id!r} in {self._directory}.{hint} "
                f"Available: {', '.join(self._paths) or '<none>'}"
            ) from None

    def find(self, path_id: str) -> GoldenPath | None:
        """Look up without raising, for the callers that genuinely want ``None``."""
        return self._paths.get(path_id)

    def all(self) -> tuple[GoldenPath, ...]:
        """Every path, including disabled ones. Sorted by id."""
        return tuple(self._paths.values())

    def enabled(self) -> tuple[GoldenPath, ...]:
        """Only the paths that may be provisioned right now.

        This is what the MCP tool list and the OSB catalog render from; a disabled path
        must not appear as a tool an agent can call, or the first thing it learns is that
        the catalog lies.
        """
        return tuple(p for p in self._paths.values() if p.enabled)

    def by_provider(self, provider: str) -> tuple[GoldenPath, ...]:
        """Every path served by one provider. The reconciler walks providers, not paths."""
        return tuple(p for p in self._paths.values() if p.provider == provider)

    def providers(self) -> tuple[str, ...]:
        """Distinct provider ids this catalog needs, sorted."""
        return tuple(sorted({p.provider for p in self._paths.values()}))

    def source_of(self, path_id: str) -> Path:
        """Which file a path came from. For error messages and the dashboard."""
        if path_id not in self._sources:
            raise UnknownGoldenPath(f"no golden path with id {path_id!r}")
        return self._sources[path_id]

    def ids(self) -> tuple[str, ...]:
        return tuple(self._paths)

    def __contains__(self, path_id: object) -> bool:
        return path_id in self._paths

    def __iter__(self) -> Iterator[GoldenPath]:
        return iter(self._paths.values())

    def __len__(self) -> int:
        return len(self._paths)

    def __repr__(self) -> str:
        return f"<Catalog {len(self._paths)} paths from {self._directory}>"
