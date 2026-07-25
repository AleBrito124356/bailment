"""The provider contract, and the HTTP machinery every real provider shares.

A provider is the only part of bailment that talks to something outside the process, so
it is also the only part that can lie to the rest of the system about reality. Three
decisions here exist to stop that happening.

**Names come in, never out.** ``external_name`` is computed and committed by the engine
*before* the provider is called, and the provider's only job is to stamp it onto the real
resource -- as the resource's name, or in a comment/tag field. If a provider invented its
own name, a crash between "create" and "record the response" would leave a resource that
nothing in the database can be matched against, and orphan detection would be reduced to
guesswork. That is also why :func:`require_managed_name` refuses a name that does not
carry the managed prefix instead of quietly adding one: rewriting the name here would
break the very correspondence the write-ahead naming exists to guarantee.

**Absence of evidence is not evidence of absence.** :class:`ResourceStatus` has three
values and ``UNKNOWN`` is a real answer. A timeout, a 500, a DNS failure -- none of those
are proof that a resource was deleted, and a provider that collapses them into ``GONE``
will cheerfully let the system mark a lease ``RELEASED`` while the resource keeps
billing. For the same reason ``list_managed`` raises on failure rather than returning an
empty list: the reconciler reads an empty list as "the account is clean".

**Errors are not free text.** Provider APIs routinely echo the request back in their
error bodies, and the request may contain a connection URI or a token. Every message that
reaches a :class:`ProviderError` goes through :func:`scrub` first, and
:class:`ProvisionResult` refuses to render its own secret payload in ``repr``, because
the single most likely way to leak a credential is a traceback, not an API response.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr

from bailment.models import utcnow

__all__ = [
    "DEFAULT_RESOURCE_PREFIX",
    "HttpProvider",
    "ManagedResource",
    "Provider",
    "ProviderConfigurationError",
    "ProviderError",
    "ProvisionResult",
    "ResourceStatus",
    "RetryPolicy",
    "coerce_bool",
    "coerce_int",
    "coerce_str",
    "is_managed_name",
    "require_managed_name",
    "resource_prefix",
    "scrub",
    "secret_text",
]


def secret_text(value: SecretStr | str | None) -> str:
    """Unwrap a configured credential to plain text, or ``""`` if it is not set.

    Providers hold credentials as plain strings because that is what an HTTP header
    needs, and the unwrapping happens exactly here so there is one place to look when
    asking how a secret gets out of :class:`~bailment.config.Settings`.
    """
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value().strip()
    return value.strip()


# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------


class ResourceStatus(StrEnum):
    """What a provider currently believes about one resource.

    ``UNKNOWN`` must never be folded into ``GONE``. See the module docstring.
    """

    EXISTS = "exists"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, repr=False)
class ProvisionResult:
    """Everything the engine needs after a successful create.

    ``outputs`` holds live credential material. It is handed straight to the engine,
    which encrypts it into a :class:`~bailment.models.Binding` and hands the caller a
    reference. It is deliberately excluded from ``repr`` -- a dataclass that prints its
    own secrets turns every unhandled exception into a credential disclosure.
    """

    provider_resource_id: str
    provider_ref: dict[str, Any]
    outputs: dict[str, str] = field(default_factory=dict)

    estimated_hourly_usd: float = 0.0
    """The provider's own cost estimate, when it knows better than the catalog.

    ``0.0`` means "no opinion" and the golden path's :class:`CostModel` is authoritative.
    """

    adopted: bool = False
    """True when ``create`` found the resource already present under our name.

    This is the idempotent-retry path: a worker crashed after the provider created the
    resource but before the response was recorded. Worth surfacing in the audit log,
    because a burst of adoptions means something upstream is retrying too eagerly.
    """

    detail: dict[str, Any] = field(default_factory=dict)
    """Non-secret extras worth keeping in the audit trail (region, host, plan...)."""

    @property
    def output_names(self) -> list[str]:
        return sorted(self.outputs)

    def __repr__(self) -> str:
        return (
            f"ProvisionResult(provider_resource_id={self.provider_resource_id!r}, "
            f"adopted={self.adopted}, outputs=<{len(self.outputs)} redacted: "
            f"{', '.join(self.output_names)}>)"
        )


@dataclass(frozen=True, slots=True)
class ManagedResource:
    """One resource found at the provider that carries bailment's marker.

    ``external_name`` is the value the provider read back off the real resource, not a
    value bailment supplied to the query. That distinction is the whole point: the
    reconciler compares what the provider says exists against what the database says
    should exist, and a provider that echoed back the filter would make every run agree
    with itself.
    """

    external_name: str
    provider_resource_id: str
    provider_ref: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]+")
_SECRETISH_KEY = re.compile(
    r"(?i)"
    r"(\"?\b(?:password|passwd|pwd|token|rest_token|read_only_rest_token|secret|"
    r"api[_-]?key|apikey|authorization|credential|connection_uri|connection_uris|"
    r"uri|url|dsn)\b\"?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;}\)\]]+)"
)
_MAX_ERROR_CHARS = 400


def scrub(text: str) -> str:
    """Redact anything credential-shaped, then truncate.

    Applied to every provider error message without exception. It is a filter, not a
    guarantee -- the real guarantee is that nothing ever puts a secret into a message on
    purpose -- but provider APIs echo requests back and this catches that class of leak.
    """
    cleaned = _URL_CREDENTIALS.sub(r"\g<scheme>***:***@", text)
    cleaned = _BEARER.sub(r"\1 ***", cleaned)
    cleaned = _SECRETISH_KEY.sub(r"\1***", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_ERROR_CHARS:
        cleaned = cleaned[:_MAX_ERROR_CHARS] + "...<truncated>"
    return cleaned


class ProviderError(Exception):
    """A provider call failed.

    ``retryable`` is the flag the worker reads to decide between backing off and giving
    up: 429 and 5xx and transport failures are retryable, 4xx is a bug in the request and
    retrying it just burns quota.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        operation: str | None = None,
    ) -> None:
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code
        self.operation = operation
        self.message = scrub(message)
        where = f"{provider}.{operation}" if operation else provider
        status = f" [http {status_code}]" if status_code is not None else ""
        super().__init__(f"{where}{status}: {self.message}")


class ProviderConfigurationError(ProviderError):
    """Credentials or required settings are missing or wrong. Never retryable."""

    def __init__(self, provider: str, message: str, *, operation: str | None = None) -> None:
        super().__init__(provider, message, retryable=False, operation=operation)


# --------------------------------------------------------------------------------------
# Managed names
# --------------------------------------------------------------------------------------

DEFAULT_RESOURCE_PREFIX = "bailment-"

#: Names travel into URL paths, query strings, DNS comments and shell-adjacent contexts.
#: Keeping the alphabet small costs nothing and removes a whole family of injection
#: questions from the review of every individual provider.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,62}$")


def resource_prefix() -> str:
    """The marker every bailment-managed resource must carry.

    A function rather than a constant because the engine that mints ``external_name`` and
    the providers that filter on it must never be able to disagree -- if they do, every
    resource looks like an orphan and the reconciler tries to delete the entire fleet.
    Anything that needs the prefix calls this.

    ``BAILMENT_RESOURCE_PREFIX`` overrides it, which two bailment installations sharing
    one cloud account need in order not to reconcile each other's resources into
    oblivion.
    """
    raw = os.environ.get("BAILMENT_RESOURCE_PREFIX", "").strip()
    return raw or DEFAULT_RESOURCE_PREFIX


def is_managed_name(name: str) -> bool:
    """Whether a name found at the provider is one bailment is allowed to touch.

    Every ``list_managed`` implementation runs its results through this, even when the
    provider-side query already filtered by prefix. The API doing the filtering is the
    same API that would happily return the whole account if a parameter name changed in
    a future version, and the blast radius of that mistake is "the reconciler deletes a
    database a human created by hand".
    """
    return bool(name) and name.startswith(resource_prefix())


def require_managed_name(provider: str, external_name: str, *, operation: str) -> str:
    """Validate a name handed down by the engine. Returns it unchanged.

    Deliberately does not repair a bad name. ``external_name`` was written to the lease
    row before this call; a provider that silently prefixed or sanitised it would create
    a resource the database cannot name, which is precisely the failure write-ahead
    naming exists to prevent.
    """
    prefix = resource_prefix()
    if not external_name.startswith(prefix):
        raise ProviderError(
            provider,
            f"external_name {external_name!r} does not start with the managed prefix "
            f"{prefix!r}; bailment refuses to create a resource it could not later "
            f"recognise as its own",
            retryable=False,
            operation=operation,
        )
    if not _NAME_RE.fullmatch(external_name):
        raise ProviderError(
            provider,
            f"external_name {external_name!r} is not a legal resource name; expected "
            f"3-63 characters of lowercase letters, digits, '.', '_' or '-'",
            retryable=False,
            operation=operation,
        )
    return external_name


# --------------------------------------------------------------------------------------
# Input coercion
# --------------------------------------------------------------------------------------
#
# Inputs arrive already validated against the golden path's JSON Schema, but a provider
# is reachable from tests, the CLI and any future caller, and "the schema checked it" is
# not a property you want to rely on when the value is about to be interpolated into a
# provider API call. These helpers re-check cheaply and fail non-retryably.


def coerce_str(
    provider: str, inputs: dict[str, Any], key: str, *, default: str | None = None
) -> str | None:
    value = inputs.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderError(
            provider, f"input {key!r} must be a string, got {type(value).__name__}", retryable=False
        )
    return value


def coerce_int(
    provider: str, inputs: dict[str, Any], key: str, *, default: int | None = None
) -> int | None:
    value = inputs.get(key, default)
    if value is None:
        return None
    # bool is an int subclass and "ttl: true" should not silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError(
            provider,
            f"input {key!r} must be an integer, got {type(value).__name__}",
            retryable=False,
        )
    return value


def coerce_bool(provider: str, inputs: dict[str, Any], key: str, *, default: bool) -> bool:
    value = inputs.get(key, default)
    if not isinstance(value, bool):
        raise ProviderError(
            provider,
            f"input {key!r} must be a boolean, got {type(value).__name__}",
            retryable=False,
        )
    return value


# --------------------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """What every provider must implement.

    The engine holds providers only through this protocol, so anything a provider knows
    that the engine needs has to be expressed here or in the returned dataclasses.
    """

    name: str
    """Registry id. Matches ``GoldenPath.provider`` and ``Lease.provider``."""

    supports_reconciliation: bool
    """Whether this provider can enumerate its own resources by bailment's marker.

    A provider that cannot stamp ``external_name`` onto the real resource -- because the
    API has no name, tag, label or comment field it controls -- must set this ``False``
    and return an empty list from ``list_managed``. The reconciler then skips the
    orphan-sweep direction for it entirely rather than concluding the account is clean.
    Being honest about the gap is fine; being silently unreconcilable is not.
    """

    def is_available(self) -> bool:
        """Whether this provider is configured well enough to be called at all."""
        ...

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None:
        """Validate without mutating. Raise :class:`ProviderError` if create would fail.

        Runs before the lease leaves PENDING so that a request doomed by a typo fails
        before anything has been named or provisioned.
        """
        ...

    async def create(
        self,
        *,
        external_name: str,
        inputs: dict[str, Any],
        declared_outputs: Sequence[str] = (),
    ) -> ProvisionResult:
        """Create the resource, stamped with ``external_name``.

        Must be idempotent: called twice with the same name it adopts the existing
        resource and returns the same reference rather than creating a second one.

        ``declared_outputs`` is the set of output names the golden path promises its
        consumers. Providers backed by a real API ignore it -- Neon returns a connection
        URI whatever the YAML wishes it were called, and the worker refuses to publish
        anything undeclared. Synthetic providers use it to produce exactly the outputs
        the path advertises, which is what lets one fake provider serve any golden path
        somebody writes against it instead of only the one it was written alongside.
        """
        ...

    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None:
        """Destroy the resource. Returning normally means it is gone.

        Destroying something already absent is success, not an error -- a retry of a
        partially-completed teardown must be able to finish cleanly. Anything else
        raises, and a raise on the teardown path is what drives a lease to ORPHANED.
        """
        ...

    async def exists(
        self, *, external_name: str, provider_ref: dict[str, Any]
    ) -> ResourceStatus: ...

    async def list_managed(self) -> list[ManagedResource]:
        """Every resource carrying bailment's marker, and nothing else.

        Raises on failure. Never returns a partial or empty list to signal an error.
        """
        ...


@runtime_checkable
class ExplainsAvailability(Protocol):
    """Optional: lets a provider say *why* it is unavailable.

    Method-only so ``isinstance`` works, and optional so a third-party provider that
    implements only :class:`Provider` still registers cleanly.
    """

    def availability_reason(self) -> str | None: ...


# --------------------------------------------------------------------------------------
# Shared HTTP behaviour
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry. Every number here is a deliberate ceiling.

    ``max_retry_after`` matters more than it looks: a provider under load can answer
    ``Retry-After: 600``, and a worker that obeys it holds its claim for ten minutes and
    stalls every other lease behind it. Past the cap we stop retrying inline and raise a
    retryable error, which hands the wait back to the lease's own backoff schedule where
    it can be observed and interrupted.
    """

    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    max_retry_after: float = 30.0


#: Statuses worth trying again. 429 for rate limits, 5xx for the provider's bad day,
#: 408/425 for a request that never really landed. 4xx is otherwise a request bug and
#: retrying it only spends quota to get the same answer.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class HttpProvider(ABC):
    """Base class for providers that talk to a REST API over httpx.

    Holds one lazily-created :class:`httpx.AsyncClient` per instance so connections are
    pooled across a worker's lifetime, and centralises the retry/backoff/scrubbing rules
    so that three provider implementations cannot end up with three different opinions
    about what a 429 means.
    """

    name: str = "http"
    supports_reconciliation: bool = True
    base_url: str = ""

    def __init__(
        self,
        *,
        timeout: httpx.Timeout | None = None,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.retry = retry or RetryPolicy()
        self.transport = transport
        """Test seam. ``httpx.MockTransport`` here lets a test drive a provider through
        the same retry, pagination and error-parsing code the real API exercises, which
        is the only way to get coverage on a 429-then-success or a 404-on-delete."""
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    # -- configuration ---------------------------------------------------------------

    @abstractmethod
    def _auth_headers(self) -> dict[str, str]:
        """Headers carrying the provider credential. Never logged, never echoed."""

    def availability_reason(self) -> str | None:
        """``None`` when usable, otherwise a sentence a human can act on."""
        return None

    def is_available(self) -> bool:
        return self.availability_reason() is None

    def _require_available(self, operation: str) -> None:
        reason = self.availability_reason()
        if reason is not None:
            raise ProviderConfigurationError(self.name, reason, operation=operation)

    # -- client lifecycle ------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=self._auth_headers(),
                    timeout=self.timeout,
                    transport=self.transport,
                    # A redirect would replay the Authorization header at whatever host
                    # the provider named. Nothing here legitimately redirects.
                    follow_redirects=False,
                )
            return self._client

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

    # -- request -------------------------------------------------------------------

    def _error_message(self, response: httpx.Response) -> str:
        """Pull a human-readable message out of an error response.

        Overridden per provider where the error envelope is known. The default keeps the
        body only as a last resort, and everything is scrubbed by ``ProviderError``.
        """
        try:
            payload = response.json()
        except ValueError:
            return response.text or f"http {response.status_code} with empty body"
        if isinstance(payload, dict):
            for key in ("message", "error", "detail", "description"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        return response.text or f"http {response.status_code}"

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            return None
        return max(0.0, (when - utcnow()).total_seconds())

    def _backoff(self, attempt: int) -> float:
        delay = min(self.retry.base_delay * (2.0**attempt), self.retry.max_delay)
        # Jitter so a fleet of workers rate-limited at the same instant does not come
        # back in lockstep. Not security-sensitive; `secrets` would buy nothing here.
        return delay * (0.5 + random.random() / 2)  # noqa: S311

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_status: Iterable[int] = (),
        extra_retry_status: Iterable[int] = (),
        operation: str = "",
    ) -> httpx.Response:
        """Issue one logical request, retrying transient failures.

        ``allow_status`` is returned to the caller instead of raising -- that is how
        ``destroy`` and ``exists`` get to see a 404 and decide for themselves what it
        means, which is the only place in the system allowed to conclude "gone".
        """
        self._require_available(operation or method.lower())
        allowed = frozenset(allow_status)
        retryable = RETRYABLE_STATUS | frozenset(extra_retry_status)
        client = await self._get_client()

        last_error: str = "no attempt was made"
        last_status: int | None = None
        for attempt in range(self.retry.max_attempts):
            try:
                response = await client.request(method, path, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                last_error = f"timeout after {self.timeout.read}s ({exc.__class__.__name__})"
                last_status = None
            except httpx.TransportError as exc:
                last_error = f"transport failure: {exc.__class__.__name__}: {exc}"
                last_status = None
            else:
                if response.status_code in allowed or response.is_success:
                    return response
                last_status = response.status_code
                last_error = self._error_message(response)
                if response.status_code not in retryable:
                    raise ProviderError(
                        self.name,
                        last_error,
                        retryable=False,
                        status_code=response.status_code,
                        operation=operation,
                    )
                hinted = self._retry_after_seconds(response)
                if hinted is not None and hinted > self.retry.max_retry_after:
                    raise ProviderError(
                        self.name,
                        f"asked to wait {hinted:.0f}s, which is longer than a worker "
                        f"should hold a claim; deferring to the lease retry schedule",
                        retryable=True,
                        status_code=response.status_code,
                        operation=operation,
                    )
                delay = hinted if hinted is not None else self._backoff(attempt)
                if attempt + 1 < self.retry.max_attempts:
                    await asyncio.sleep(delay)
                continue

            if attempt + 1 < self.retry.max_attempts:
                await asyncio.sleep(self._backoff(attempt))

        raise ProviderError(
            self.name,
            f"giving up after {self.retry.max_attempts} attempts: {last_error}",
            retryable=True,
            status_code=last_status,
            operation=operation,
        )

    def _json_object(self, response: httpx.Response, *, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name,
                f"response was not JSON: {exc}",
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            ) from None
        if not isinstance(payload, dict):
            raise ProviderError(
                self.name,
                f"expected a JSON object, got {type(payload).__name__}",
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            )
        return payload

    def _json_list(self, response: httpx.Response, *, operation: str) -> list[Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name,
                f"response was not JSON: {exc}",
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            ) from None
        if not isinstance(payload, list):
            raise ProviderError(
                self.name,
                f"expected a JSON array, got {type(payload).__name__}",
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            )
        return payload

    async def __aenter__(self) -> HttpProvider:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def parse_timestamp(raw: object) -> datetime | None:
    """Best-effort ISO-8601 to an aware datetime. Never returns a naive one.

    Provider timestamps are decoration -- they help a human judge how long an orphan has
    been burning money -- so an unparseable value is dropped rather than raised on.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
