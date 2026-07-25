"""Cloudflare DNS records.

DNS is the smallest useful thing to lease and the easiest to get catastrophically wrong.
A record is free, takes a second to create, and expires perfectly well -- but the same
API call that publishes a preview hostname can also point a company's apex at an attacker
or delegate the zone away. So this provider is deliberately the most restrictive of the
four:

* one zone, from configuration, never from request inputs -- a record created in a zone
  the reconciler does not scan is an orphan that can never be found;
* a small allow-list of record types. ``A``, ``AAAA``, ``CNAME`` and ``TXT`` affect one
  hostname. ``NS`` delegates a subtree, ``MX`` redirects mail, ``CAA`` decides who may
  issue certificates for the domain. Those are not capabilities to hand an agent by
  default, and turning them on should be a deliberate act by whoever runs the broker;
* the zone apex is refused unless explicitly enabled. Cloudflare will happily accept a
  second apex ``A`` record and round-robin traffic to it, which takes half a site down
  without erroring at any point.

The marker lives in the record ``comment`` field, set to ``external_name`` exactly. That
is what makes DNS reconcilable at all: record *names* are chosen by the requester and
cannot be trusted to carry a prefix, so the comment is the only field bailment fully
controls. ``list_managed`` filters on it both server-side and again locally.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
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
    coerce_int,
    coerce_str,
    is_managed_name,
    parse_timestamp,
    require_managed_name,
    resource_prefix,
    secret_text,
)

__all__ = ["CloudflareProvider"]

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

#: Cloudflare's documented error codes for "that record is not there" and "that record
#: is already there". Matching on the code rather than the prose keeps the idempotency
#: logic from breaking the day someone rewords an error message.
_RECORD_NOT_FOUND_CODES = frozenset({81044, 81043, 7003})
_RECORD_EXISTS_CODES = frozenset({81053, 81057, 81058})

_SAFE_TYPES = frozenset({"A", "AAAA", "CNAME", "TXT"})
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?!-)[a-z0-9_*-]{1,63}(?:\.(?!-)[a-z0-9_-]{1,63})*\.?$"
)
_PER_PAGE = 100
_MAX_PAGES = 50


class CloudflareProvider(HttpProvider):
    """Provisions DNS records in one configured Cloudflare zone."""

    name = "cloudflare"
    supports_reconciliation = True
    base_url = CLOUDFLARE_API_BASE

    def __init__(
        self,
        *,
        api_token: str | None = None,
        zone_id: str | None = None,
        allowed_types: frozenset[str] | None = None,
        allow_apex: bool = False,
        timeout: httpx.Timeout | None = None,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(timeout=timeout, retry=retry, transport=transport)
        settings = get_settings()
        self.api_token = (
            api_token if api_token is not None else secret_text(settings.cloudflare_api_token)
        )
        self.zone_id = zone_id if zone_id is not None else secret_text(settings.cloudflare_zone_id)
        self.allowed_types = allowed_types or _SAFE_TYPES
        self.allow_apex = allow_apex
        self._zone_name: str | None = None

    # -- configuration ---------------------------------------------------------------

    def availability_reason(self) -> str | None:
        if not self.api_token:
            return "BAILMENT_CLOUDFLARE_API_TOKEN is not set"
        if not self.zone_id:
            return (
                "BAILMENT_CLOUDFLARE_ZONE_ID is not set; bailment writes records into "
                "one zone so that the reconciler has a bounded place to look"
            )
        return None

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- envelope handling -----------------------------------------------------------

    def _error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text or f"http {response.status_code} with empty body"
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                parts = [
                    f"{item.get('code')}: {item.get('message')}"
                    for item in errors
                    if isinstance(item, dict)
                ]
                if parts:
                    return "; ".join(parts)
        return response.text or f"http {response.status_code}"

    @staticmethod
    def _error_codes(response: httpx.Response) -> frozenset[int]:
        try:
            payload = response.json()
        except ValueError:
            return frozenset()
        if not isinstance(payload, dict):
            return frozenset()
        errors = payload.get("errors")
        if not isinstance(errors, list):
            return frozenset()
        codes = {
            item["code"]
            for item in errors
            if isinstance(item, dict) and isinstance(item.get("code"), int)
        }
        return frozenset(codes)

    def _envelope(self, response: httpx.Response, *, operation: str) -> dict[str, Any]:
        payload = self._json_object(response, operation=operation)
        if payload.get("success") is not True:
            # Cloudflare can answer 200 with success:false. Trusting the status code
            # alone would turn a refusal into a recorded provision.
            raise ProviderError(
                self.name,
                self._error_message(response),
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            )
        return payload

    # -- zone ------------------------------------------------------------------------

    async def _zone(self, *, operation: str) -> str:
        """The zone's apex name, cached for the life of the provider instance."""
        if self._zone_name is not None:
            return self._zone_name
        response = await self._request(
            "GET", f"/zones/{self.zone_id}", allow_status=(404,), operation=operation
        )
        if response.status_code == 404:
            raise ProviderError(
                self.name,
                f"zone {self.zone_id!r} does not exist or the token cannot see it",
                retryable=False,
                status_code=404,
                operation=operation,
            )
        payload = self._envelope(response, operation=operation)
        result = payload.get("result")
        zone_name = result.get("name") if isinstance(result, dict) else None
        if not isinstance(zone_name, str) or not zone_name:
            raise ProviderError(
                self.name, "zone response has no name", retryable=False, operation=operation
            )
        self._zone_name = zone_name.lower()
        return self._zone_name

    # -- input validation ------------------------------------------------------------

    def _record_type(self, inputs: dict[str, Any], *, operation: str) -> str:
        raw = coerce_str(self.name, inputs, "type", default="A") or "A"
        record_type = raw.strip().upper()
        if record_type not in self.allowed_types:
            raise ProviderError(
                self.name,
                f"record type {record_type!r} is not in this broker's allow-list "
                f"({', '.join(sorted(self.allowed_types))}); types that redirect mail or "
                f"delegate the zone are opt-in for a reason",
                retryable=False,
                operation=operation,
            )
        return record_type

    def _record_name(self, inputs: dict[str, Any], *, operation: str) -> str:
        raw = coerce_str(self.name, inputs, "name")
        if not raw:
            raise ProviderError(
                self.name, "input 'name' is required", retryable=False, operation=operation
            )
        name = raw.strip().rstrip(".").lower()
        if not _HOSTNAME_RE.fullmatch(name):
            raise ProviderError(
                self.name,
                f"input 'name' is not a valid hostname: {raw!r}",
                retryable=False,
                operation=operation,
            )
        return name

    def _content(self, inputs: dict[str, Any], record_type: str, *, operation: str) -> str:
        content = coerce_str(self.name, inputs, "content")
        if not content:
            raise ProviderError(
                self.name, "input 'content' is required", retryable=False, operation=operation
            )
        if record_type in ("A", "AAAA"):
            try:
                address = ipaddress.ip_address(content.strip())
            except ValueError:
                raise ProviderError(
                    self.name,
                    f"content for a {record_type} record must be an IP address",
                    retryable=False,
                    operation=operation,
                ) from None
            want = 4 if record_type == "A" else 6
            if address.version != want:
                raise ProviderError(
                    self.name,
                    f"content for a {record_type} record must be IPv{want}",
                    retryable=False,
                    operation=operation,
                )
            return str(address)
        if record_type == "CNAME":
            target = content.strip().rstrip(".").lower()
            if not _HOSTNAME_RE.fullmatch(target):
                raise ProviderError(
                    self.name,
                    "content for a CNAME record must be a hostname",
                    retryable=False,
                    operation=operation,
                )
            return target
        if len(content) > 2048:
            raise ProviderError(
                self.name,
                "TXT content exceeds 2048 characters",
                retryable=False,
                operation=operation,
            )
        return content

    def _ttl(self, inputs: dict[str, Any], *, operation: str) -> int:
        # 1 is Cloudflare's "automatic". A short TTL is the right default for a leased
        # record: when the lease ends the record disappears, and a resolver holding a
        # day-long cache entry makes the teardown look like it did not work.
        ttl = coerce_int(self.name, inputs, "ttl", default=60)
        assert ttl is not None
        if ttl != 1 and not (30 <= ttl <= 86400):
            raise ProviderError(
                self.name,
                "ttl must be 1 (automatic) or between 30 and 86400 seconds",
                retryable=False,
                operation=operation,
            )
        return ttl

    async def _validated_record(self, inputs: dict[str, Any], *, operation: str) -> dict[str, Any]:
        record_type = self._record_type(inputs, operation=operation)
        name = self._record_name(inputs, operation=operation)
        zone_name = await self._zone(operation=operation)

        if name != zone_name and not name.endswith("." + zone_name):
            raise ProviderError(
                self.name,
                f"{name!r} is not inside the managed zone {zone_name!r}",
                retryable=False,
                operation=operation,
            )
        if name == zone_name and not self.allow_apex:
            raise ProviderError(
                self.name,
                f"refusing to create a record at the zone apex {zone_name!r}; an extra "
                f"apex record silently round-robins production traffic. Enable "
                f"allow_apex on the provider if this is really what you want",
                retryable=False,
                operation=operation,
            )

        return {
            "type": record_type,
            "name": name,
            "content": self._content(inputs, record_type, operation=operation),
            "ttl": self._ttl(inputs, operation=operation),
            "proxied": coerce_bool(self.name, inputs, "proxied", default=False),
        }

    # -- record helpers --------------------------------------------------------------

    def _record_id_from_ref(self, provider_ref: dict[str, Any]) -> str | None:
        value = provider_ref.get("record_id")
        return value if isinstance(value, str) and value else None

    async def _get_record(self, record_id: str, *, operation: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            f"/zones/{self.zone_id}/dns_records/{record_id}",
            allow_status=(400, 404),
            operation=operation,
        )
        if response.status_code in (400, 404):
            if self._error_codes(response) & _RECORD_NOT_FOUND_CODES or response.status_code == 404:
                return None
            raise ProviderError(
                self.name,
                self._error_message(response),
                retryable=False,
                status_code=response.status_code,
                operation=operation,
            )
        payload = self._envelope(response, operation=operation)
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    async def _list_records(
        self, *, comment_exact: str | None = None, operation: str
    ) -> list[dict[str, Any]]:
        """Records in the zone carrying bailment's comment marker.

        The ``comment.startswith`` filter is applied server-side to keep the response
        small, and every caller filters again locally. If a future API version quietly
        ignores an unknown query parameter, the server-side filter becomes a no-op and
        this function would otherwise return the entire zone to a reconciler that is
        allowed to delete what it is given.
        """
        records: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            params: dict[str, Any] = {"page": page, "per_page": _PER_PAGE}
            if comment_exact is not None:
                params["comment.exact"] = comment_exact
            else:
                params["comment.startswith"] = resource_prefix()
            response = await self._request(
                "GET", f"/zones/{self.zone_id}/dns_records", params=params, operation=operation
            )
            payload = self._envelope(response, operation=operation)
            result = payload.get("result")
            if not isinstance(result, list):
                raise ProviderError(
                    self.name,
                    "dns_records response did not contain a result array",
                    retryable=False,
                    operation=operation,
                )
            records.extend(item for item in result if isinstance(item, dict))
            info = payload.get("result_info")
            total_pages = info.get("total_pages") if isinstance(info, dict) else None
            if not isinstance(total_pages, int) or page >= total_pages:
                break
        return records

    async def _record_by_marker(
        self, external_name: str, *, operation: str
    ) -> dict[str, Any] | None:
        found = await self._list_records(comment_exact=external_name, operation=operation)
        matches = [record for record in found if record.get("comment") == external_name]
        if not matches:
            return None
        if len(matches) > 1:
            raise ProviderError(
                self.name,
                f"{len(matches)} DNS records carry the marker {external_name!r}; bailment "
                f"will not guess which one a lease refers to",
                retryable=False,
                operation=operation,
            )
        return matches[0]

    def _result(
        self, record: dict[str, Any], external_name: str, *, adopted: bool, operation: str
    ) -> ProvisionResult:
        record_id = record.get("id")
        record_name = record.get("name")
        if not isinstance(record_id, str) or not isinstance(record_name, str):
            raise ProviderError(
                self.name,
                "dns record response is missing 'id' or 'name'",
                retryable=False,
                operation=operation,
            )
        content = record.get("content")
        return ProvisionResult(
            provider_resource_id=record_id,
            provider_ref={
                "zone_id": self.zone_id,
                "record_id": record_id,
                "record_name": record_name,
                "comment": external_name,
            },
            # Nothing here is secret -- a DNS record is public by construction -- but it
            # still travels as an output so the consumer gets it the same way it gets
            # every other value, through the binding rather than the API response.
            outputs={
                "DNS_RECORD_NAME": record_name,
                "DNS_RECORD_CONTENT": content if isinstance(content, str) else "",
                "DNS_RECORD_FQDN": record_name,
            },
            adopted=adopted,
            detail={
                "type": record.get("type"),
                "ttl": record.get("ttl"),
                "proxied": record.get("proxied"),
            },
        )

    # -- the contract ----------------------------------------------------------------

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None:
        operation = "preflight"
        self._require_available(operation)
        require_managed_name(self.name, external_name, operation=operation)
        await self._validated_record(inputs, operation=operation)
        await self._record_by_marker(external_name, operation=operation)

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

        existing = await self._record_by_marker(external_name, operation=operation)
        if existing is not None:
            return self._result(existing, external_name, adopted=True, operation=operation)

        body = await self._validated_record(inputs, operation=operation)
        body["comment"] = external_name

        response = await self._request(
            "POST",
            f"/zones/{self.zone_id}/dns_records",
            json_body=body,
            allow_status=(400, 409),
            operation=operation,
        )
        if response.status_code in (400, 409):
            if not self._error_codes(response) & _RECORD_EXISTS_CODES:
                raise ProviderError(
                    self.name,
                    self._error_message(response),
                    retryable=False,
                    status_code=response.status_code,
                    operation=operation,
                )
            raced = await self._record_by_marker(external_name, operation=operation)
            if raced is None:
                # A record with that hostname exists but is not ours -- somebody else
                # owns the name. Creating anything here would fight a human for a
                # hostname, so it fails loudly and the lease never goes ACTIVE.
                raise ProviderError(
                    self.name,
                    f"a DNS record for {body['name']!r} already exists and was not "
                    f"created by bailment",
                    retryable=False,
                    status_code=response.status_code,
                    operation=operation,
                )
            return self._result(raced, external_name, adopted=True, operation=operation)

        payload = self._envelope(response, operation=operation)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProviderError(
                self.name,
                "create dns_record response had no result object",
                retryable=False,
                operation=operation,
            )
        return self._result(result, external_name, adopted=False, operation=operation)

    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None:
        operation = "destroy"
        self._require_available(operation)

        record_id = self._record_id_from_ref(provider_ref)
        if record_id is None:
            found = await self._record_by_marker(external_name, operation=operation)
            if found is None:
                return
            candidate = found.get("id")
            if not isinstance(candidate, str):
                raise ProviderError(
                    self.name,
                    f"found a record marked {external_name!r} with no usable id",
                    retryable=False,
                    operation=operation,
                )
            record_id = candidate

        record = await self._get_record(record_id, operation=operation)
        if record is None:
            return
        if record.get("comment") != external_name:
            # Somebody edited the comment, or the id now belongs to a different record.
            # Either way our proof of ownership is gone, and deleting a DNS record we
            # cannot prove we own is how an outage gets attributed to "the automation".
            raise ProviderError(
                self.name,
                f"record {record_id} no longer carries the marker {external_name!r}; "
                f"refusing to delete it",
                retryable=False,
                operation=operation,
            )

        response = await self._request(
            "DELETE",
            f"/zones/{self.zone_id}/dns_records/{record_id}",
            allow_status=(400, 404),
            operation=operation,
        )
        if response.status_code in (400, 404):
            if self._error_codes(response) & _RECORD_NOT_FOUND_CODES or response.status_code == 404:
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
            record_id = self._record_id_from_ref(provider_ref)
            if record_id is None:
                found = await self._record_by_marker(external_name, operation=operation)
                return ResourceStatus.GONE if found is None else ResourceStatus.EXISTS
            record = await self._get_record(record_id, operation=operation)
        except ProviderError as exc:
            if exc.retryable:
                return ResourceStatus.UNKNOWN
            raise
        return ResourceStatus.GONE if record is None else ResourceStatus.EXISTS

    async def list_managed(self) -> list[ManagedResource]:
        operation = "list_managed"
        self._require_available(operation)
        managed: list[ManagedResource] = []
        for record in await self._list_records(operation=operation):
            comment = record.get("comment")
            record_id = record.get("id")
            record_name = record.get("name")
            if not isinstance(comment, str) or not isinstance(record_id, str):
                continue
            # The local re-check. See _list_records for why this is not redundant.
            if not is_managed_name(comment):
                continue
            managed.append(
                ManagedResource(
                    external_name=comment,
                    provider_resource_id=record_id,
                    provider_ref={
                        "zone_id": self.zone_id,
                        "record_id": record_id,
                        "record_name": record_name,
                        "comment": comment,
                    },
                    created_at=parse_timestamp(record.get("created_on")),
                    detail={
                        "name": record_name,
                        "type": record.get("type"),
                        "ttl": record.get("ttl"),
                        "proxied": record.get("proxied"),
                    },
                )
            )
        return managed
