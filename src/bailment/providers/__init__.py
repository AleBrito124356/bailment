"""Providers: the only code in bailment that touches something outside the process.

Everything above this package -- the API, the engine, the workers, the reconciler --
deals in leases, and a lease is a claim about a resource somewhere else. These modules
are where that claim is made true or found to be false, so they are held to a narrower
contract than the rest of the codebase:

* names are handed down, never invented (:func:`~bailment.providers.base.require_managed_name`),
* ``UNKNOWN`` is a real answer and never rounded to ``GONE``,
* ``list_managed`` returns only what carries bailment's marker, and raises rather than
  returning an empty list when it cannot tell,
* every mutating call is idempotent under retry,
* nothing that could be a credential reaches a log line or an exception message.

:mod:`bailment.providers.memory` implements all of it in-process and can be told to fail
in the specific ways that make the lifecycle interesting.
"""

from __future__ import annotations

from bailment.providers.base import (
    DEFAULT_RESOURCE_PREFIX,
    ExplainsAvailability,
    HttpProvider,
    ManagedResource,
    Provider,
    ProviderConfigurationError,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
    RetryPolicy,
    is_managed_name,
    require_managed_name,
    resource_prefix,
    scrub,
)
from bailment.providers.cloudflare import CloudflareProvider
from bailment.providers.memory import MemoryProvider, MemoryResource, default_memory_provider
from bailment.providers.neon import NeonProvider
from bailment.providers.registry import (
    ProviderRegistry,
    ProviderStatus,
    UnknownProvider,
    build_default_registry,
    default_registry,
)
from bailment.providers.upstash import UpstashProvider

__all__ = [
    "DEFAULT_RESOURCE_PREFIX",
    "CloudflareProvider",
    "ExplainsAvailability",
    "HttpProvider",
    "ManagedResource",
    "MemoryProvider",
    "MemoryResource",
    "NeonProvider",
    "Provider",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderRegistry",
    "ProviderStatus",
    "ProvisionResult",
    "ResourceStatus",
    "RetryPolicy",
    "UnknownProvider",
    "UpstashProvider",
    "build_default_registry",
    "default_memory_provider",
    "default_registry",
    "is_managed_name",
    "require_managed_name",
    "resource_prefix",
    "scrub",
]
