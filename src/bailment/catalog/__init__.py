"""The golden path catalog: what this installation is willing to hand out.

:mod:`bailment.catalog.schema` defines the shape of a golden path and is the contract
every other surface derives from. :mod:`bailment.catalog.loader` reads a directory of
YAML files into a validated :class:`~bailment.catalog.loader.Catalog`.

The four paths shipped in ``paths/`` are real, not samples. ``sandbox`` runs against the
built-in memory provider and needs no cloud account, which is how somebody evaluating
this project gets to watch a lease expire and destroy its resource within a few minutes
of cloning the repo.
"""

from __future__ import annotations

from bailment.catalog.loader import (
    Catalog,
    CatalogError,
    UnknownGoldenPath,
    aload_catalog,
    load_catalog,
    load_default_catalog,
)
from bailment.catalog.schema import (
    BindingOutput,
    CostModel,
    Duration,
    GoldenPath,
    LeasePolicy,
    PolicyRule,
    format_duration,
    parse_duration,
)

__all__ = [
    "BindingOutput",
    "Catalog",
    "CatalogError",
    "CostModel",
    "Duration",
    "GoldenPath",
    "LeasePolicy",
    "PolicyRule",
    "UnknownGoldenPath",
    "aload_catalog",
    "format_duration",
    "load_catalog",
    "load_default_catalog",
    "parse_duration",
]
