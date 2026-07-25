"""Upstash Redis databases.

Serverless Redis, created in a couple of seconds, billed per request. A good leased
resource for an agent that needs a cache, a queue or a rate limiter for the length of one
task, and a good demonstration of the difference between a credential and a capability:
what comes back here is a fresh database with its own password, not a shared production
Redis with a token somebody rotated last year.

The database name is the marker -- Upstash has no tag field -- so ``external_name`` is
used verbatim as the name and ``list_managed`` filters on the managed prefix. Upstash
also happily creates two databases with the same name, so the adopt-on-retry check in
:meth:`create` is not an optimisation: without it, one retried provision leaves a second
paid-for database that no lease will ever tear down.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from bailment.config import get_settings
from bailment.providers.base import (
    HttpProvider,
    ManagedResource,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
    RetryPolicy,
    coerce_bool,
    coerce_str,
    is_managed_name,
    require_managed_name,
    secret_text,
)

__all__ = ["UpstashProvider"]

UPSTASH_API_BASE = "https://api.upstash.com/v2/redis"

#: Upstash does not use a consistent status code for "no such database" -- a delete of a
#: database that is already gone can come back 400 with a text body. Matching on the text
#: is unpleasant, but the alternative is treating every 400 as "gone", which would let a
#: quota error or a malformed id masquerade as a successful teardown.
_NOT_FOUND_RE = re.compile(r"(?i)\b(not\s*found|does\s*not\s*exist|no\s*such)\b")

_DEFAULT_REGION = "global"
_DEFAULT_PRIMARY_REGION = "us-east-1"
_REGION_RE = re.compile(r"^[a-z0-9-]{2,32}$")


class UpstashProvider(HttpProvider):
    """Provisions Upstash Redis databases under one account."""

    name = "upstash"
    supports_reconciliation = True
    base_url = UPSTASH_API_BASE

    def __init__(
        self,
        *,
        email: str | None = None,
        api_key: str | None = None,
        timeout: httpx.Timeout | None = None,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(timeout=timeout, retry=retry, transport=transport)
        settings = get_settings()
        self.email = email if email is not None else secret_text(settings.upstash_email)
        self.api_key = api_key if api_key is not None else secret_text(settings.upstash_api_key)

    # -- configuration ---------------------------------------------------------------

    def availability_reason(self) -> str | None:
        if not self.email:
            return "BAILMENT_UPSTASH_EMAIL is not set"
        if not self.api_key:
            return "BAILMENT_UPSTASH_API_KEY is not set"
        return None

    def _auth_headers(self) -> dict[str, str]:
        raw = f"{self.email}:{self.api_key}".encode()
        return {
            "Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- helpers ---------------------------------------------------------------------

    @staticmethod
    def _database_name(database: dict[str, Any]) -> str | None:
        name = database.get("database_name")
        return name if isinstance(name, str) else None

    @staticmethod
    def _database_id(database: dict[str, Any]) -> str | None:
        value = database.get("database_id")
        return value if isinstance(value, str) and value else None

    def _database_id_from_ref(self, provider_ref: dict[str, Any]) -> str | None:
        value = provider_ref.get("database_id")
        return value if isinstance(value, str) and value else None

    async def _list_databases(self, *, operation: str) -> list[dict[str, Any]]:
        response = await self._request("GET", "/databases", operation=operation)
        payload = self._json_list(response, operation=operation)
        return [item for item in payload if isinstance(item, dict)]

    async def _database_by_name(self, name: str, *, operation: str) -> dict[str, Any] | None:
        matches = [
            db
            for db in await self._list_databases(operation=operation)
            if self._database_name(db) == name
        ]
        if not matches:
            return None
        if len(matches) > 1:
            # Two databases with our name means an earlier retry already double
            # provisioned. Guessing which one the binding refers to would hand the caller
            # a credential for the wrong database, so this stops and asks for a human.
            raise ProviderError(
                self.name,
                f"{len(matches)} databases are named {name!r}; bailment cannot tell which "
                f"one a lease refers to, so it will not touch either",
                retryable=False,
                operation=operation,
            )
        return matches[0]

    async def _get_database(self, database_id: str, *, operation: str) -> dict[str, Any] | None:
        """The database, or ``None`` if Upstash says it is gone. Raises otherwise."""
        response = await self._request(
            "GET",
            f"/database/{database_id}",
            allow_status=(400, 404),
            operation=operation,
        )
        if response.status_code in (400, 404):
            if response.status_code == 404 or _NOT_FOUND_RE.search(response.text or ""):
                return None
            raise ProviderError(
                self.name,
                self._error_message(response),
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            )
        return self._json_object(response, operation=operation)

    def _outputs(self, database: dict[str, Any], *, operation: str) -> dict[str, str]:
        endpoint = database.get("endpoint")
        password = database.get("password")
        port = database.get("port")
        if not isinstance(endpoint, str) or not isinstance(password, str):
            raise ProviderError(
                self.name,
                "database record is missing its endpoint or password",
                retryable=False,
                operation=operation,
            )
        tls = database.get("tls")
        scheme = "redis" if tls is False else "rediss"
        port_number = port if isinstance(port, int) else 6379
        outputs = {
            "REDIS_URL": f"{scheme}://default:{password}@{endpoint}:{port_number}",
            "REDIS_HOST": endpoint,
            "REDIS_PORT": str(port_number),
            "REDIS_PASSWORD": password,
            "UPSTASH_REDIS_REST_URL": f"https://{endpoint}",
        }
        rest_token = database.get("rest_token")
        if isinstance(rest_token, str) and rest_token:
            outputs["UPSTASH_REDIS_REST_TOKEN"] = rest_token
        read_only = database.get("read_only_rest_token")
        if isinstance(read_only, str) and read_only:
            outputs["UPSTASH_REDIS_READONLY_REST_TOKEN"] = read_only
        return outputs

    def _result(
        self, database: dict[str, Any], *, adopted: bool, operation: str
    ) -> ProvisionResult:
        database_id = self._database_id(database)
        name = self._database_name(database)
        if database_id is None or name is None:
            raise ProviderError(
                self.name,
                "database record is missing 'database_id' or 'database_name'",
                retryable=False,
                operation=operation,
            )
        return ProvisionResult(
            provider_resource_id=database_id,
            provider_ref={"database_id": database_id, "database_name": name},
            outputs=self._outputs(database, operation=operation),
            adopted=adopted,
            detail={
                "region": database.get("region"),
                "primary_region": database.get("primary_region"),
                "type": database.get("database_type"),
                "state": database.get("state"),
            },
        )

    @staticmethod
    def _created_at(database: dict[str, Any]) -> datetime | None:
        raw = database.get("creation_time")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    def _read_regions(self, inputs: dict[str, Any], *, operation: str) -> list[str]:
        raw = inputs.get("read_regions", [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ProviderError(
                self.name,
                "input 'read_regions' must be an array of region names",
                retryable=False,
                operation=operation,
            )
        regions: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not _REGION_RE.fullmatch(item):
                raise ProviderError(
                    self.name,
                    f"read_regions contains an invalid region name: {item!r}",
                    retryable=False,
                    operation=operation,
                )
            regions.append(item)
        return regions

    def _region_inputs(self, inputs: dict[str, Any], *, operation: str) -> tuple[str, str]:
        region = coerce_str(self.name, inputs, "region", default=_DEFAULT_REGION) or _DEFAULT_REGION
        primary = (
            coerce_str(self.name, inputs, "primary_region", default=_DEFAULT_PRIMARY_REGION)
            or _DEFAULT_PRIMARY_REGION
        )
        for value, label in ((region, "region"), (primary, "primary_region")):
            if not _REGION_RE.fullmatch(value):
                raise ProviderError(
                    self.name,
                    f"input {label!r} is not a valid region name: {value!r}",
                    retryable=False,
                    operation=operation,
                )
        return region, primary

    # -- the contract ----------------------------------------------------------------

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None:
        operation = "preflight"
        self._require_available(operation)
        require_managed_name(self.name, external_name, operation=operation)
        self._region_inputs(inputs, operation=operation)
        self._read_regions(inputs, operation=operation)
        coerce_bool(self.name, inputs, "eviction", default=False)
        # Cheapest call that proves the credentials work and the account is reachable.
        # Also surfaces the duplicate-name condition before anything is created.
        await self._database_by_name(external_name, operation=operation)

    async def create(
        self,
        *,
        external_name: str,
        inputs: dict[str, Any],
        declared_outputs: Sequence[str] = (),
    ) -> ProvisionResult:
        # declared_outputs is part of the Provider contract but means nothing here: this
        # resource's outputs are whatever the API hands back, not whatever the YAML hoped
        # for. The worker publishes only the intersection with the path's declarations.
        del declared_outputs
        operation = "create"
        require_managed_name(self.name, external_name, operation=operation)

        existing = await self._database_by_name(external_name, operation=operation)
        if existing is not None:
            database_id = self._database_id(existing)
            if database_id is None:
                raise ProviderError(
                    self.name,
                    f"database named {external_name!r} has no id",
                    retryable=False,
                    operation=operation,
                )
            # The list response omits the password on some plans; re-read the single
            # database so an adopted retry still returns a usable credential.
            detailed = await self._get_database(database_id, operation=operation) or existing
            return self._result(detailed, adopted=True, operation=operation)

        region, primary_region = self._region_inputs(inputs, operation=operation)
        body: dict[str, Any] = {
            "name": external_name,
            "region": region,
            "tls": True,
            "eviction": coerce_bool(self.name, inputs, "eviction", default=False),
        }
        if region == "global":
            body["primary_region"] = primary_region
            read_regions = self._read_regions(inputs, operation=operation)
            if read_regions:
                body["read_regions"] = read_regions

        response = await self._request("POST", "/database", json_body=body, operation=operation)
        created = self._json_object(response, operation=operation)
        return self._result(created, adopted=False, operation=operation)

    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None:
        operation = "destroy"
        self._require_available(operation)

        database_id = self._database_id_from_ref(provider_ref)
        if database_id is None:
            # No id recorded: the worker died inside the create window. The name is the
            # only handle left, which is exactly what write-ahead naming is for.
            found = await self._database_by_name(external_name, operation=operation)
            if found is None:
                return
            database_id = self._database_id(found)
            if database_id is None:
                raise ProviderError(
                    self.name,
                    f"found a database named {external_name!r} with no usable id",
                    retryable=False,
                    operation=operation,
                )

        current = await self._get_database(database_id, operation=operation)
        if current is None:
            return
        current_name = self._database_name(current)
        if current_name != external_name:
            raise ProviderError(
                self.name,
                f"database {database_id} is now named {current_name!r}, not "
                f"{external_name!r}; refusing to delete a resource bailment can no "
                f"longer prove it owns",
                retryable=False,
                operation=operation,
            )

        response = await self._request(
            "DELETE",
            f"/database/{database_id}",
            allow_status=(400, 404),
            operation=operation,
        )
        if response.status_code in (400, 404):
            if response.status_code == 404 or _NOT_FOUND_RE.search(response.text or ""):
                # Raced with another teardown. Already gone is the outcome we wanted.
                return
            raise ProviderError(
                self.name,
                self._error_message(response),
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            )

    async def exists(self, *, external_name: str, provider_ref: dict[str, Any]) -> ResourceStatus:
        operation = "exists"
        if not self.is_available():
            return ResourceStatus.UNKNOWN
        try:
            database_id = self._database_id_from_ref(provider_ref)
            if database_id is None:
                found = await self._database_by_name(external_name, operation=operation)
                return ResourceStatus.GONE if found is None else ResourceStatus.EXISTS
            current = await self._get_database(database_id, operation=operation)
        except ProviderError as exc:
            if exc.retryable:
                return ResourceStatus.UNKNOWN
            raise
        return ResourceStatus.GONE if current is None else ResourceStatus.EXISTS

    async def list_managed(self) -> list[ManagedResource]:
        operation = "list_managed"
        self._require_available(operation)
        managed: list[ManagedResource] = []
        for database in await self._list_databases(operation=operation):
            name = self._database_name(database)
            database_id = self._database_id(database)
            if name is None or database_id is None:
                continue
            # Everything in this account that is not prefixed belongs to a human. The
            # reconciler is allowed to delete what this function returns, so the filter
            # is the last thing standing between a sweep and somebody's production cache.
            if not is_managed_name(name):
                continue
            managed.append(
                ManagedResource(
                    external_name=name,
                    provider_resource_id=database_id,
                    provider_ref={"database_id": database_id, "database_name": name},
                    created_at=self._created_at(database),
                    detail={
                        "region": database.get("region"),
                        "type": database.get("database_type"),
                        "state": database.get("state"),
                    },
                )
            )
        return managed
