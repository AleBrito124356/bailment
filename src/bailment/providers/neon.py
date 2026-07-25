"""Neon Postgres branches.

A Neon branch is close to the ideal leased resource: copy-on-write from the parent, so
creating one is seconds rather than minutes, and deleting one actually reclaims the
storage. An agent gets a real Postgres with real data shaped like production, for four
hours, and then it is gone.

Two constraints shape this file.

**One project, from configuration.** The project id comes from the environment and
cannot be overridden per request. An agent that could name its own project could create
a branch somewhere ``list_managed`` never looks, and a resource outside the reconciler's
field of view is worse than no resource at all -- it is a bill nobody will ever explain.
The same reasoning applies to the Cloudflare zone.

**The branch name is the marker.** Neon branches have no tag or label field, so
``external_name`` is used verbatim as the branch name and ``list_managed`` filters on the
managed prefix. That is also why ``destroy`` re-reads the branch before deleting it: we
delete by id, and if the name at that id no longer matches the name we recorded, someone
has renamed or replaced the branch and it is no longer ours to delete.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

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
    secret_text,
)

__all__ = ["NeonProvider"]

NEON_API_BASE = "https://console.neon.tech/api/v2"

#: Neon answers 423 while a project or branch is mid-operation. It is a "come back in a
#: moment", not a refusal, so it joins the retryable set for this provider only.
_EXTRA_RETRY = (423,)

_MAX_PAGES = 50


class NeonProvider(HttpProvider):
    """Provisions Neon database branches under one configured project."""

    name = "neon"
    supports_reconciliation = True
    base_url = NEON_API_BASE

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project_id: str | None = None,
        timeout: httpx.Timeout | None = None,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(timeout=timeout, retry=retry, transport=transport)
        settings = get_settings()
        self.api_key = api_key if api_key is not None else secret_text(settings.neon_api_key)
        self.project_id = (
            project_id if project_id is not None else secret_text(settings.neon_project_id)
        )

    # -- configuration ---------------------------------------------------------------

    def availability_reason(self) -> str | None:
        if not self.api_key:
            return "BAILMENT_NEON_API_KEY is not set"
        if not self.project_id:
            return (
                "BAILMENT_NEON_PROJECT_ID is not set; bailment creates branches inside "
                "one project so that the reconciler has a bounded place to look"
            )
        return None

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- helpers ---------------------------------------------------------------------

    async def _list_branches(self, *, operation: str) -> list[dict[str, Any]]:
        """Every branch in the configured project, following cursor pagination.

        No prefix filter here: Neon has no server-side name filter worth trusting, so
        the filtering happens in :meth:`list_managed` where it is visible.
        """
        branches: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = await self._request(
                "GET",
                f"/projects/{self.project_id}/branches",
                params=params,
                extra_retry_status=_EXTRA_RETRY,
                operation=operation,
            )
            payload = self._json_object(response, operation=operation)
            page = payload.get("branches")
            if not isinstance(page, list):
                raise ProviderError(
                    self.name,
                    "list branches response did not contain a 'branches' array",
                    retryable=False,
                    operation=operation,
                )
            typed_page = [item for item in page if isinstance(item, dict)]
            branches.extend(typed_page)
            pagination = payload.get("pagination")
            next_cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
            if not isinstance(next_cursor, str) or not next_cursor or len(typed_page) < 100:
                break
            cursor = next_cursor
        return branches

    async def _branch_by_name(self, name: str, *, operation: str) -> dict[str, Any] | None:
        for branch in await self._list_branches(operation=operation):
            if branch.get("name") == name:
                return branch
        return None

    async def _get_branch(self, branch_id: str, *, operation: str) -> dict[str, Any] | None:
        """The branch, or ``None`` if Neon says it is not there. Raises on anything else."""
        response = await self._request(
            "GET",
            f"/projects/{self.project_id}/branches/{branch_id}",
            allow_status=(404,),
            extra_retry_status=_EXTRA_RETRY,
            operation=operation,
        )
        if response.status_code == 404:
            return None
        payload = self._json_object(response, operation=operation)
        branch = payload.get("branch")
        return branch if isinstance(branch, dict) else None

    async def _default_database_and_role(
        self, branch_id: str, *, operation: str
    ) -> tuple[str, str]:
        """The first database on the branch and its owner role.

        Neon names the initial database and role from the project's settings, so they
        are discovered rather than assumed -- guessing ``neondb``/``neondb_owner`` works
        until it does not, and then it fails at connection-uri time on somebody's demo.
        """
        response = await self._request(
            "GET",
            f"/projects/{self.project_id}/branches/{branch_id}/databases",
            extra_retry_status=_EXTRA_RETRY,
            operation=operation,
        )
        payload = self._json_object(response, operation=operation)
        databases = payload.get("databases")
        if not isinstance(databases, list) or not databases:
            raise ProviderError(
                self.name,
                f"branch {branch_id} has no databases; cannot build a connection string",
                retryable=False,
                operation=operation,
            )
        first = databases[0]
        if not isinstance(first, dict):
            raise ProviderError(
                self.name, "malformed database entry", retryable=False, operation=operation
            )
        db_name = first.get("name")
        owner = first.get("owner_name")
        if not isinstance(db_name, str) or not isinstance(owner, str):
            raise ProviderError(
                self.name,
                "database entry is missing 'name' or 'owner_name'",
                retryable=False,
                operation=operation,
            )
        return db_name, owner

    async def _connection_uri(
        self, branch_id: str, database: str, role: str, *, pooled: bool, operation: str
    ) -> str:
        response = await self._request(
            "GET",
            f"/projects/{self.project_id}/connection_uri",
            params={
                "branch_id": branch_id,
                "database_name": database,
                "role_name": role,
                "pooled": "true" if pooled else "false",
            },
            extra_retry_status=_EXTRA_RETRY,
            operation=operation,
        )
        payload = self._json_object(response, operation=operation)
        uri = payload.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ProviderError(
                self.name,
                "connection_uri response did not contain a 'uri'",
                retryable=False,
                operation=operation,
            )
        return uri

    @staticmethod
    def _outputs_from_uri(uri: str) -> dict[str, str]:
        """Split a connection URI into the environment variables tooling expects.

        Every value here is secret-adjacent and several are the secret itself; they go
        straight into the binding ciphertext and never anywhere else.
        """
        parts = urlsplit(uri)
        outputs = {"DATABASE_URL": uri}
        if parts.hostname:
            outputs["PGHOST"] = parts.hostname
        if parts.port:
            outputs["PGPORT"] = str(parts.port)
        if parts.username:
            outputs["PGUSER"] = parts.username
        if parts.password:
            outputs["PGPASSWORD"] = parts.password
        database = parts.path.lstrip("/")
        if database:
            outputs["PGDATABASE"] = database
        return outputs

    async def _resolve_parent(self, inputs: dict[str, Any], *, operation: str) -> str | None:
        """Turn a ``parent_branch`` name or id into a branch id.

        ``None`` means "let Neon use the project's default branch", which is what most
        callers want and what the golden path should default to.
        """
        parent_id = coerce_str(self.name, inputs, "parent_id")
        if parent_id:
            branch = await self._get_branch(parent_id, operation=operation)
            if branch is None:
                raise ProviderError(
                    self.name,
                    f"parent_id {parent_id!r} does not exist in project {self.project_id}",
                    retryable=False,
                    operation=operation,
                )
            return parent_id
        parent_name = coerce_str(self.name, inputs, "parent_branch")
        if not parent_name:
            return None
        branch = await self._branch_by_name(parent_name, operation=operation)
        if branch is None:
            raise ProviderError(
                self.name,
                f"parent_branch {parent_name!r} does not exist in project {self.project_id}",
                retryable=False,
                operation=operation,
            )
        branch_id = branch.get("id")
        if not isinstance(branch_id, str):
            raise ProviderError(
                self.name, "parent branch has no id", retryable=False, operation=operation
            )
        return branch_id

    def _branch_id_from_ref(self, provider_ref: dict[str, Any]) -> str | None:
        value = provider_ref.get("branch_id")
        return value if isinstance(value, str) and value else None

    async def _build_result(
        self,
        branch: dict[str, Any],
        inputs: dict[str, Any],
        *,
        adopted: bool,
        connection_uri: str | None,
        operation: str,
    ) -> ProvisionResult:
        branch_id = branch.get("id")
        branch_name = branch.get("name")
        if not isinstance(branch_id, str) or not isinstance(branch_name, str):
            raise ProviderError(
                self.name,
                "branch response is missing 'id' or 'name'",
                retryable=False,
                operation=operation,
            )
        pooled = coerce_bool(self.name, inputs, "pooled", default=False)
        database = coerce_str(self.name, inputs, "database_name")
        role = coerce_str(self.name, inputs, "role_name")

        # The create call already hands back a connection URI for the branch's default
        # database, and that URI states which database and role it is for. Reading it is
        # two fewer round trips than asking Neon, and it cannot disagree with the
        # credential we are about to hand out -- which a separately-fetched answer can.
        if connection_uri is not None and not pooled and database is None and role is None:
            parts = urlsplit(connection_uri)
            database = parts.path.lstrip("/") or None
            role = parts.username or None

        if database is None or role is None:
            discovered_db, discovered_role = await self._default_database_and_role(
                branch_id, operation=operation
            )
            database = database or discovered_db
            role = role or discovered_role
            connection_uri = None

        if connection_uri is None or pooled:
            connection_uri = await self._connection_uri(
                branch_id, database, role, pooled=pooled, operation=operation
            )
        return ProvisionResult(
            provider_resource_id=branch_id,
            provider_ref={
                "project_id": self.project_id,
                "branch_id": branch_id,
                "branch_name": branch_name,
                "database_name": database,
                "role_name": role,
            },
            outputs=self._outputs_from_uri(connection_uri),
            adopted=adopted,
            detail={
                "branch_name": branch_name,
                "database_name": database,
                "role_name": role,
                "pooled": pooled,
                "parent_id": branch.get("parent_id"),
            },
        )

    # -- the contract ----------------------------------------------------------------

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None:
        operation = "preflight"
        self._require_available(operation)
        require_managed_name(self.name, external_name, operation=operation)

        coerce_str(self.name, inputs, "database_name")
        coerce_str(self.name, inputs, "role_name")
        coerce_bool(self.name, inputs, "pooled", default=False)
        suspend = coerce_int(self.name, inputs, "suspend_timeout_seconds")
        if suspend is not None and not (0 <= suspend <= 604800):
            raise ProviderError(
                self.name,
                "suspend_timeout_seconds must be between 0 and 604800",
                retryable=False,
                operation=operation,
            )

        response = await self._request(
            "GET",
            f"/projects/{self.project_id}",
            allow_status=(404,),
            extra_retry_status=_EXTRA_RETRY,
            operation=operation,
        )
        if response.status_code == 404:
            raise ProviderError(
                self.name,
                f"project {self.project_id!r} does not exist or the API key cannot see it",
                retryable=False,
                status_code=404,
                operation=operation,
            )

        await self._resolve_parent(inputs, operation=operation)

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

        existing = await self._branch_by_name(external_name, operation=operation)
        if existing is not None:
            # A branch already carries this name, which can only mean a previous attempt
            # got further than its caller knew. Adopt it; creating a second one would
            # leave the first unreferenced and unbilled-to-anybody.
            return await self._build_result(
                existing, inputs, adopted=True, connection_uri=None, operation=operation
            )

        branch: dict[str, Any] = {"name": external_name}
        parent_id = await self._resolve_parent(inputs, operation=operation)
        if parent_id:
            branch["parent_id"] = parent_id

        endpoint: dict[str, Any] = {"type": "read_write"}
        suspend = coerce_int(self.name, inputs, "suspend_timeout_seconds")
        if suspend is not None:
            endpoint["suspend_timeout_seconds"] = suspend

        response = await self._request(
            "POST",
            f"/projects/{self.project_id}/branches",
            json_body={"branch": branch, "endpoints": [endpoint]},
            allow_status=(409,),
            extra_retry_status=_EXTRA_RETRY,
            operation=operation,
        )
        if response.status_code == 409:
            # Lost a race with our own retry, or with another worker holding a stale
            # claim. Re-read rather than fail: the resource we wanted now exists.
            raced = await self._branch_by_name(external_name, operation=operation)
            if raced is None:
                raise ProviderError(
                    self.name,
                    f"neon reported a conflict creating branch {external_name!r} but no "
                    f"branch with that name exists",
                    retryable=True,
                    status_code=409,
                    operation=operation,
                )
            return await self._build_result(
                raced, inputs, adopted=True, connection_uri=None, operation=operation
            )

        payload = self._json_object(response, operation=operation)
        created = payload.get("branch")
        if not isinstance(created, dict):
            raise ProviderError(
                self.name,
                "create branch response did not contain a 'branch' object",
                retryable=False,
                operation=operation,
            )

        connection_uri: str | None = None
        uris = payload.get("connection_uris")
        if isinstance(uris, list) and uris and isinstance(uris[0], dict):
            candidate = uris[0].get("connection_uri")
            if isinstance(candidate, str) and candidate:
                connection_uri = candidate

        return await self._build_result(
            created, inputs, adopted=False, connection_uri=connection_uri, operation=operation
        )

    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None:
        operation = "destroy"
        self._require_available(operation)

        branch_id = self._branch_id_from_ref(provider_ref)
        if branch_id is None:
            # The write-ahead name earns its keep here: the worker died before it could
            # record the branch id, and the name is the only thing that can find it.
            found = await self._branch_by_name(external_name, operation=operation)
            if found is None:
                return
            candidate = found.get("id")
            if not isinstance(candidate, str):
                raise ProviderError(
                    self.name,
                    f"found branch named {external_name!r} with no usable id",
                    retryable=False,
                    operation=operation,
                )
            branch_id = candidate

        branch = await self._get_branch(branch_id, operation=operation)
        if branch is None:
            return

        current_name = branch.get("name")
        if current_name != external_name:
            # Deleting by id alone would be enough to destroy whatever now lives at that
            # id. Refusing leaves the lease ORPHANED, which is visible and fixable; the
            # alternative is invisible and is somebody's production branch.
            raise ProviderError(
                self.name,
                f"branch {branch_id} is now named {current_name!r}, not {external_name!r}; "
                f"refusing to delete a resource bailment can no longer prove it owns",
                retryable=False,
                operation=operation,
            )

        response = await self._request(
            "DELETE",
            f"/projects/{self.project_id}/branches/{branch_id}",
            allow_status=(404,),
            extra_retry_status=_EXTRA_RETRY,
            operation=operation,
        )
        if response.status_code == 404:
            # Raced with another teardown. Already gone is the outcome we wanted.
            return

    async def exists(self, *, external_name: str, provider_ref: dict[str, Any]) -> ResourceStatus:
        operation = "exists"
        if not self.is_available():
            return ResourceStatus.UNKNOWN
        try:
            branch_id = self._branch_id_from_ref(provider_ref)
            if branch_id is None:
                found = await self._branch_by_name(external_name, operation=operation)
                return ResourceStatus.GONE if found is None else ResourceStatus.EXISTS
            branch = await self._get_branch(branch_id, operation=operation)
        except ProviderError as exc:
            # A 500 or a timeout says nothing about whether the branch is there. Saying
            # GONE here is how a live resource gets marked RELEASED and disappears from
            # every report while it keeps billing.
            if exc.retryable:
                return ResourceStatus.UNKNOWN
            raise
        return ResourceStatus.GONE if branch is None else ResourceStatus.EXISTS

    async def list_managed(self) -> list[ManagedResource]:
        operation = "list_managed"
        self._require_available(operation)
        managed: list[ManagedResource] = []
        for branch in await self._list_branches(operation=operation):
            name = branch.get("name")
            branch_id = branch.get("id")
            if not isinstance(name, str) or not isinstance(branch_id, str):
                continue
            # The prefix check is the whole safety story for the orphan sweep. Neon has
            # no way to mark a branch as ours other than its name, so anything not
            # carrying the prefix is somebody else's and must never be reported.
            if not is_managed_name(name):
                continue
            managed.append(
                ManagedResource(
                    external_name=name,
                    provider_resource_id=branch_id,
                    provider_ref={
                        "project_id": self.project_id,
                        "branch_id": branch_id,
                        "branch_name": name,
                    },
                    created_at=parse_timestamp(branch.get("created_at")),
                    detail={
                        "parent_id": branch.get("parent_id"),
                        "protected": branch.get("protected"),
                        "state": branch.get("current_state"),
                    },
                )
            )
        return managed
