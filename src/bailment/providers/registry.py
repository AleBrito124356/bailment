"""Provider lookup and availability reporting.

The registry keeps providers that cannot run. That is the whole design.

An installation with no Cloudflare token should not behave as though Cloudflare does not
exist -- it should say "cloudflare: CLOUDFLARE_API_TOKEN is not set". The difference
matters in two places. A golden path referencing an unconfigured provider must fail at
startup validation with a sentence an operator can act on, not with a KeyError that reads
like a typo in the catalog. And the reconciler must be able to tell "this provider has no
orphans" apart from "this provider was never asked", because the second one silently
looks like the first in every dashboard that only lists what is working.

So :meth:`ProviderRegistry.get` returns unavailable providers too, and every call path
that is about to touch a real API goes through :meth:`ProviderRegistry.require_available`
and gets a :class:`ProviderConfigurationError` naming the missing setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bailment.providers.base import (
    ExplainsAvailability,
    Provider,
    ProviderConfigurationError,
)
from bailment.providers.cloudflare import CloudflareProvider
from bailment.providers.memory import default_memory_provider
from bailment.providers.neon import NeonProvider
from bailment.providers.upstash import UpstashProvider

__all__ = [
    "ProviderRegistry",
    "ProviderStatus",
    "UnknownProvider",
    "build_default_registry",
    "default_registry",
]


class UnknownProvider(LookupError):
    """A golden path or lease names a provider nobody registered."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        super().__init__(
            f"unknown provider {name!r}; registered providers are "
            f"{', '.join(known) if known else '<none>'}"
        )


@runtime_checkable
class _SupportsAclose(Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """One row of the availability report shown in the CLI and the dashboard."""

    name: str
    available: bool
    reason: str | None
    supports_reconciliation: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "reason": self.reason,
            "supports_reconciliation": self.supports_reconciliation,
        }


class ProviderRegistry:
    """Name to provider, plus a report of what is actually usable."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider, *, replace: bool = False) -> None:
        if not provider.name:
            raise ValueError("provider has no name")
        if provider.name in self._providers and not replace:
            raise ValueError(
                f"provider {provider.name!r} is already registered; pass replace=True if "
                f"that is deliberate"
            )
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        """The provider, configured or not. Raises :class:`UnknownProvider` if absent."""
        try:
            return self._providers[name]
        except KeyError:
            raise UnknownProvider(name, self.names()) from None

    def require_available(self, name: str) -> Provider:
        """The provider, or a configuration error naming the setting that is missing."""
        provider = self.get(name)
        if not provider.is_available():
            raise ProviderConfigurationError(name, self._reason(provider) or "not configured")
        return provider

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def available_names(self) -> list[str]:
        return sorted(name for name, p in self._providers.items() if p.is_available())

    def reconcilable(self) -> list[Provider]:
        """Providers the reconciler can sweep for orphans.

        Excludes unconfigured ones -- a provider that cannot be called has no answer
        about what exists, and an empty answer is the one thing the reconciler must
        never invent. Also excludes providers that admit they cannot tag their
        resources, since ``list_managed`` on those returns nothing by contract.
        """
        return [
            provider
            for _, provider in sorted(self._providers.items())
            if provider.is_available() and provider.supports_reconciliation
        ]

    @staticmethod
    def _reason(provider: Provider) -> str | None:
        if isinstance(provider, ExplainsAvailability):
            return provider.availability_reason()
        return None if provider.is_available() else "not configured"

    def report(self) -> list[ProviderStatus]:
        return [
            ProviderStatus(
                name=name,
                available=provider.is_available(),
                reason=self._reason(provider),
                supports_reconciliation=provider.supports_reconciliation,
            )
            for name, provider in sorted(self._providers.items())
        ]

    async def aclose(self) -> None:
        """Close every provider that holds connections. Safe to call more than once."""
        for provider in self._providers.values():
            if isinstance(provider, _SupportsAclose):
                await provider.aclose()


def build_default_registry() -> ProviderRegistry:
    """A registry holding all four built-in providers, configured from settings.

    All of them, unconditionally, including the ones with no credentials. There is no
    "enabled providers" list on purpose: a provider that is registered-but-unavailable
    produces the sentence naming the variable somebody forgot to set, and a provider that
    was never registered produces ``UnknownProvider``, which reads like a typo in the
    catalog and sends the reader looking in the wrong file.
    """
    registry = ProviderRegistry()
    registry.register(default_memory_provider())
    registry.register(NeonProvider())
    registry.register(UpstashProvider())
    registry.register(CloudflareProvider())
    return registry


_DEFAULT: ProviderRegistry | None = None


def default_registry() -> ProviderRegistry:
    """The process-wide registry.

    Cached because the HTTP providers hold connection pools and the memory provider holds
    the fake cloud; building a new registry per request would throw both away.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_default_registry()
    return _DEFAULT
