"""The dashboard-facing REST API.

Every handler here is thin on purpose. Deciding, naming, committing and enforcing
visibility all happen in :class:`bailment.engine.service.LeaseService`, which the MCP
server and the CLI also call, and a handler that reached past it into
:mod:`bailment.models` would be a second implementation of rules that must have exactly
one. What this module owns is the HTTP-shaped part: which tier may call what, how a
:class:`~bailment.engine.service.ServiceError` becomes a status code, and pagination.

**The rule this module is built around.** No response body may contain a decrypted secret
value, under any code path, for any caller. An operator token is *permitted* to decrypt --
:attr:`bailment.engine.service.Caller.may_read_secrets` is true for
:attr:`~bailment.engine.service.CallerKind.OPERATOR` -- so the service would not stop a
handler that asked. What stops it is that no handler asks, and
:func:`assert_handlers_never_decrypt` proves that at import time by looking at the compiled
code of every function in this module for the names that lead to plaintext. A handler that
calls ``resolve_binding``, touches a sealed column or builds a
:class:`~bailment.secrets.SecretBox` makes the API fail to start.

That guard also shapes the code in a way worth knowing before you edit it: the binding
endpoint selects its columns explicitly rather than loading whole ``Binding`` rows,
because naming the sealed column at all would trip the check. The sealed envelope never
leaves the database.

**Two tiers, enforced in :mod:`bailment.api.deps`.** Operator sees and does everything.
Agent requests leases and reads its own, and every list endpoint an agent can reach is
scoped by the service rather than by a query parameter -- "show me everything" is the most
natural thing in the world for an agent to try.

**Where pagination is honest.** Lists ask the service for one row more than the page size
and report ``has_more``. There is no ``total``, because counting would mean writing the
visibility predicate a second time in a second place, and the failure mode of those two
drifting apart is somebody seeing a lease that is not theirs. The one place an aggregate
genuinely is the product -- ``/stats`` -- applies the predicate exactly once, in
:func:`_visible_to`, which is annotated to say it must agree with
:meth:`~bailment.engine.service.LeaseService.list_leases`.

**Reconciling from HTTP never destroys anything.** :meth:`bailment.engine.reconciler.
Reconciler.may_destroy` needs two switches: the global setting and a per-provider
allowlist. This module always passes an empty allowlist, so the trigger endpoint reports
orphans and cannot delete one however the deployment is configured. Destroying is a
deliberate act at a terminal with a confirmation, not a POST somebody can be talked into.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Iterator, Sequence
from datetime import timedelta
from types import CodeType, ModuleType
from typing import Annotated, Any, Final, TypeVar

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import ColumnElement, func, or_, select

from bailment.api.deps import (
    CallerDep,
    CatalogDep,
    OperatorDep,
    RegistryDep,
    ServiceDep,
    SessionDep,
    SessionmakerDep,
    SettingsDep,
)
from bailment.api.schemas import (
    HOURS_PER_MONTH,
    USAGE_HINT,
    ApprovalQueueResponse,
    ApproveRequestBody,
    AuditEventResponse,
    AuditTrailResponse,
    BindingListResponse,
    BindingResponse,
    CatalogDetailResponse,
    CatalogEntryResponse,
    CatalogListResponse,
    ErrorDetail,
    ErrorEnvelope,
    LeasePage,
    LeaseResponse,
    PendingApprovalResponse,
    ProviderListResponse,
    ProviderStatusResponse,
    ProvisionRequestBody,
    ProvisionResponse,
    ReconcileRunDetailResponse,
    ReconcileRunPage,
    ReconcileRunResponse,
    ReconcileTriggerResponse,
    RejectRequestBody,
    RenewRequestBody,
    RevokeRequestBody,
    StatsResponse,
    http_error,
)
from bailment.catalog.loader import UnknownGoldenPath
from bailment.engine.reconciler import Reconciler
from bailment.engine.service import (
    BindingNotFound,
    Caller,
    ConflictingState,
    InvalidRequest,
    LeaseNotFound,
    NotAuthorized,
    PathDisabled,
    ProviderUnavailable,
    ProvisionRequest,
    RenewalRefused,
    SecretAccessDenied,
    ServiceError,
    UnknownPath,
    aware,
)
from bailment.logging import get_logger
from bailment.models import Approval, AuditEvent, Binding, Lease, ReconcileRun, utcnow
from bailment.providers.registry import ProviderRegistry
from bailment.states import LIVE_STATES, USABLE_STATES, LeaseState

__all__ = [
    "API_PREFIX",
    "DECRYPTION_NAMES",
    "GUARDED_MODULES",
    "assert_handlers_never_decrypt",
    "install_error_handlers",
    "referenced_names",
    "router",
]

log = get_logger("bailment.api.routes")

#: Versioned because the dashboard, the CLI and any third-party client all hard-code it,
#: and a broker that renames its endpoints breaks agents that are mid-task.
API_PREFIX: Final = "/api/v1"

router: Final = APIRouter(prefix=API_PREFIX)

T = TypeVar("T")

LimitQuery = Annotated[int, Query(ge=1, le=200, description="Rows per page.")]
OffsetQuery = Annotated[int, Query(ge=0, description="Rows to skip.")]

#: Errors every endpoint can produce, for the generated OpenAPI document.
_AUTH_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorEnvelope, "description": "No usable bearer token."},
    403: {"model": ErrorEnvelope, "description": "Authenticated, but not permitted."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorEnvelope, "description": "No such object, or not visible to you."}
}


# --------------------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------------------


def _http_error(exc: ServiceError) -> HTTPException:
    """Map a service error onto a status code.

    Ordered most specific first, because several of these are subclasses of each other:
    ``SecretAccessDenied`` is a ``NotAuthorized`` and ``RenewalRefused`` is a
    ``ConflictingState``, and getting the order wrong would flatten the specific message
    into the general one.

    ``UnknownPath`` and ``PathDisabled`` deliberately produce the same answer. A disabled
    golden path is not something a caller may provision, and telling an agent that a
    capability exists but is switched off invites it to keep retrying in case it comes
    back.
    """
    if isinstance(exc, InvalidRequest):
        return http_error(status.HTTP_400_BAD_REQUEST, "invalid_request", str(exc), exc.problems)
    if isinstance(exc, SecretAccessDenied):
        # Unreachable: no handler in this module asks for a value. If it ever fires, some
        # surface built the wrong caller kind, which is a defect in bailment rather than a
        # misbehaving client -- so it is logged at error level rather than merely returned.
        log.error("a REST handler reached the secret boundary", error=str(exc))
        return http_error(status.HTTP_403_FORBIDDEN, "secret_access_denied", str(exc))
    if isinstance(exc, NotAuthorized):
        return http_error(status.HTTP_403_FORBIDDEN, "not_authorized", str(exc))
    if isinstance(exc, UnknownPath | PathDisabled):
        return http_error(status.HTTP_404_NOT_FOUND, "unknown_golden_path", str(exc))
    if isinstance(exc, LeaseNotFound):
        return http_error(status.HTTP_404_NOT_FOUND, "lease_not_found", str(exc))
    if isinstance(exc, BindingNotFound):
        return http_error(status.HTTP_404_NOT_FOUND, "binding_not_found", str(exc))
    if isinstance(exc, RenewalRefused):
        return http_error(status.HTTP_409_CONFLICT, "renewal_refused", str(exc))
    if isinstance(exc, ConflictingState):
        return http_error(status.HTTP_409_CONFLICT, "conflicting_state", str(exc))
    if isinstance(exc, ProviderUnavailable):
        return http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "provider_unavailable", str(exc))
    log.error("unmapped service error", error_type=type(exc).__name__, error=str(exc))
    return http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "service_error", str(exc))


def install_error_handlers(app: FastAPI) -> None:
    """Give an application the same error envelope this router produces. Optional.

    The router does not need it: every handler translates its own service errors, so
    mounting the router alone already answers 404 for a missing lease rather than 500.
    Two things sit outside a handler's reach, though, and both produce a differently
    shaped body if nobody catches them.

    *Request validation.* FastAPI answers a malformed body with its own
    ``{"detail": [{...}]}`` list, which is a second error shape for a client to parse.
    Flattening it into :class:`~bailment.api.schemas.ErrorDetail` means there is one.

    *A service error that escaped.* Nothing should reach here, because every call goes
    through :func:`_call`. It is registered anyway: the failure mode it covers is a future
    handler that forgets the wrapper, and the difference between a 409 and a 500 is the
    difference between a client that retries sensibly and one that pages somebody.
    """

    async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
        del request
        problems: list[str] = []
        if isinstance(exc, RequestValidationError):
            for error in exc.errors():
                # 'body' leads every location and says nothing; the field after it is the
                # part the caller has to fix.
                location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
                problems.append(f"{location or '<request>'}: {error.get('msg', 'invalid')}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": ErrorDetail(
                    error="invalid_request",
                    message="the request is not valid for this endpoint",
                    problems=problems,
                ).model_dump()
            },
        )

    async def _on_service_error(request: Request, exc: Exception) -> JSONResponse:
        del request
        if not isinstance(exc, ServiceError):  # pragma: no cover - registered by type
            raise exc
        log.warning(
            "a service error reached the application handler",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        translated = _http_error(exc)
        return JSONResponse(status_code=translated.status_code, content=translated.detail)

    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(ServiceError, _on_service_error)


async def _call(awaitable: Awaitable[T]) -> T:
    """Await a service call, translating its refusals into HTTP.

    Every handler wraps its service calls in this rather than relying on an application
    level exception handler, so the router is correct on its own. An application that
    mounts it without installing handlers still returns 404 for a missing lease instead of
    500.
    """
    try:
        return await awaitable
    except ServiceError as exc:
        raise _http_error(exc) from None


def _parse_states(raw: Sequence[str] | None) -> list[LeaseState] | None:
    """Turn repeated (or comma-separated) ``?state=`` values into lease states.

    An unknown state is a 400 listing the real ones rather than an empty result set: a
    filter nobody can spell that silently matches nothing reads exactly like "there are no
    leases", which is the worst possible answer for a dashboard to give.
    """
    if not raw:
        return None
    states: list[LeaseState] = []
    unknown: list[str] = []
    for item in raw:
        for part in item.split(","):
            token = part.strip().lower()
            if not token:
                continue
            try:
                states.append(LeaseState(token))
            except ValueError:
                unknown.append(part.strip())
    if unknown:
        raise http_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            f"unknown lease state(s): {', '.join(unknown)}. Valid states are "
            f"{', '.join(s.value for s in LeaseState)}.",
        )
    return states or None


def _page_bounds(rows: Sequence[T], limit: int, offset: int) -> tuple[list[T], bool, int | None]:
    """Split a ``limit + 1`` result into a page, a flag and the next offset."""
    has_more = len(rows) > limit
    return list(rows[:limit]), has_more, (offset + limit if has_more else None)


def _provider_available(registry: ProviderRegistry, name: str) -> bool:
    return name in registry and registry.get(name).is_available()


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------


@router.get(
    "/catalog",
    tags=["catalog"],
    summary="Everything this installation is willing to hand out",
    responses=_AUTH_RESPONSES,
)
async def list_catalog(
    caller: CallerDep, catalog: CatalogDep, registry: RegistryDep
) -> CatalogListResponse:
    """List golden paths.

    Operators see disabled paths too, flagged; nobody else does. A disabled path must not
    appear to a caller who could try to provision it, because the first thing that caller
    learns is that the catalog lies.
    """
    paths = catalog.all() if caller.is_operator else catalog.enabled()
    return CatalogListResponse(
        items=[
            CatalogEntryResponse.from_path(
                path, provider_available=_provider_available(registry, path.provider)
            )
            for path in paths
        ],
        count=len(paths),
        directory=str(catalog.directory) if caller.is_operator else None,
    )


@router.get(
    "/catalog/{golden_path_id}",
    tags=["catalog"],
    summary="One golden path, with its policy chain",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_catalog_entry(
    golden_path_id: str, caller: CallerDep, catalog: CatalogDep, registry: RegistryDep
) -> CatalogDetailResponse:
    path = catalog.find(golden_path_id)
    if path is None or (not path.enabled and not caller.is_operator):
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "unknown_golden_path",
            f"no golden path with id {golden_path_id!r}. Available: "
            f"{', '.join(p.id for p in catalog.enabled()) or '<none>'}",
        )
    source: str | None = None
    if caller.is_operator:
        try:
            # The file name only. The full path would publish the layout of the machine.
            source = catalog.source_of(path.id).name
        except UnknownGoldenPath:  # pragma: no cover - the path came from this catalog
            source = None
    return CatalogDetailResponse.from_path_detail(
        path,
        provider_available=_provider_available(registry, path.provider),
        source=source,
    )


# --------------------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------------------


@router.post(
    "/leases",
    tags=["leases"],
    status_code=status.HTTP_201_CREATED,
    summary="Request a lease",
    response_description="The lease, plus any notices that teach the caller its bounds.",
    responses={
        200: {"model": ProvisionResponse, "description": "An idempotency key was replayed."},
        400: {"model": ErrorEnvelope, "description": "The request failed the path's schema."},
        **_AUTH_RESPONSES,
        **_NOT_FOUND,
        503: {"model": ErrorEnvelope, "description": "The path's provider is unavailable."},
    },
)
async def create_lease(
    body: ProvisionRequestBody,
    response: Response,
    caller: CallerDep,
    service: ServiceDep,
    idempotency_header: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Same meaning as the body field. Send one or the other.",
        ),
    ] = None,
) -> ProvisionResponse:
    """Accept a provisioning request.

    Nothing here decides anything: :meth:`~bailment.engine.service.LeaseService.
    request_provision` resolves the path, validates inputs, honours the idempotency key,
    clamps the TTL, evaluates policy and commits the resource's name before any worker can
    reach a provider. This handler's only judgements are which status code the outcome
    deserves and where ``Location`` points.

    A replay answers 200 rather than 201, because 201 asserts that something was created
    and an idempotent replay is the promise that nothing was.

    The idempotency key may arrive in the body or in the ``Idempotency-Key`` header. Two
    different values is a refusal rather than a preference: a caller that sent both meant
    one of them, and guessing which would decide whether this call provisions a second
    resource.
    """
    key = body.idempotency_key or idempotency_header
    if body.idempotency_key and idempotency_header and body.idempotency_key != idempotency_header:
        raise http_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            "the request carries two different idempotency keys, one in the body and one "
            "in the Idempotency-Key header. Send one.",
        )
    outcome = await _call(
        service.request_provision(
            caller,
            ProvisionRequest(
                golden_path_id=body.golden_path,
                inputs=body.inputs,
                ttl=body.ttl,
                idempotency_key=key,
            ),
        )
    )
    if outcome.replayed:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"{API_PREFIX}/leases/{outcome.lease.id}"
    return ProvisionResponse.from_outcome(outcome)


@router.get(
    "/leases",
    tags=["leases"],
    summary="List leases",
    responses=_AUTH_RESPONSES,
)
async def list_leases(
    caller: CallerDep,
    service: ServiceDep,
    state: Annotated[
        list[str] | None,
        Query(description="Repeatable, or comma-separated. E.g. state=active&state=expiring."),
    ] = None,
    golden_path: Annotated[str | None, Query(description="Filter by golden path id.")] = None,
    provider: Annotated[str | None, Query(description="Filter by provider id.")] = None,
    requester: Annotated[
        str | None,
        Query(description="Filter by principal. Ignored for anyone but an operator."),
    ] = None,
    live: Annotated[
        bool, Query(description="Only leases believed to have a real resource behind them.")
    ] = False,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> LeasePage:
    """Newest first.

    ``requester`` is applied by the service and only for a caller who may see everything;
    anybody else gets their own leases whatever they pass. The filter is not trusted from
    the caller, it is imposed on them.
    """
    views = await _call(
        service.list_leases(
            caller,
            states=_parse_states(state),
            golden_path_id=golden_path,
            provider=provider,
            requester=requester,
            live_only=live,
            # One row more than the page, so ``has_more`` needs no second query and no
            # second copy of the visibility predicate. See the module docstring.
            limit=limit + 1,
            offset=offset,
        )
    )
    items, has_more, next_offset = _page_bounds(views, limit, offset)
    return LeasePage(
        items=[LeaseResponse.from_view(view) for view in items],
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=next_offset,
    )


@router.get(
    "/leases/{lease_id}",
    tags=["leases"],
    summary="One lease",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_lease(lease_id: str, caller: CallerDep, service: ServiceDep) -> LeaseResponse:
    """A lease this caller may see.

    A caller who may not see it gets 404 rather than 403, and that is the service's
    decision: confirming that a lease id is real to somebody who cannot read it is an
    enumeration oracle.
    """
    return LeaseResponse.from_view(await _call(service.get_lease(lease_id, caller)))


@router.post(
    "/leases/{lease_id}/renew",
    tags=["leases"],
    summary="Extend a live lease",
    responses={
        **_AUTH_RESPONSES,
        **_NOT_FOUND,
        409: {"model": ErrorEnvelope, "description": "Not renewable, exhausted, or too late."},
    },
)
async def renew_lease(
    lease_id: str, body: RenewRequestBody, caller: CallerDep, service: ServiceDep
) -> ProvisionResponse:
    """Renew from now, never from the old deadline, and never earlier than it already was.

    The arithmetic lives in :func:`bailment.engine.leases.renew_lease` because the ticker
    and this endpoint have to agree about what a renewal does to ``expires_at``, and two
    implementations of that is two answers.
    """
    return ProvisionResponse.from_outcome(
        await _call(service.renew(lease_id, caller, ttl=body.ttl))
    )


@router.post(
    "/leases/{lease_id}/revoke",
    tags=["leases"],
    summary="End a lease early and queue its resource for destruction",
    responses={
        **_AUTH_RESPONSES,
        **_NOT_FOUND,
        409: {"model": ErrorEnvelope, "description": "Already finished."},
    },
)
async def revoke_lease(
    lease_id: str, body: RevokeRequestBody, caller: CallerDep, service: ServiceDep
) -> LeaseResponse:
    """Revoke.

    The state this produces is an *intention*: ``REVOKED`` means "there is a resource and
    it must die", and only a worker that called the provider and got a clean answer may
    write ``RELEASED``. That separation is what keeps a failed teardown visible.

    A request that never reached a provider is rejected rather than revoked, which the
    service decides -- putting a row in the teardown queue with nothing behind it would
    manufacture an orphan out of a cancellation.
    """
    return LeaseResponse.from_view(
        await _call(service.revoke(lease_id, caller, reason=body.reason))
    )


@router.post(
    "/leases/{lease_id}/retry-teardown",
    tags=["leases"],
    summary="Re-queue an orphaned lease for teardown",
    responses={
        **_AUTH_RESPONSES,
        **_NOT_FOUND,
        409: {"model": ErrorEnvelope, "description": "The lease is not orphaned."},
    },
)
async def retry_teardown(lease_id: str, caller: OperatorDep, service: ServiceDep) -> LeaseResponse:
    """Ask for another destroy attempt on a lease that gave up.

    Orphans are not retried automatically, deliberately: they got there by exhausting
    their budget against a provider that kept refusing, and a loop that keeps calling it
    hides the problem instead of surfacing it. This is the button a human presses once
    they have fixed whatever the provider was complaining about.
    """
    return LeaseResponse.from_view(await _call(service.retry_teardown(lease_id, caller)))


@router.get(
    "/leases/{lease_id}/bindings",
    tags=["leases"],
    summary="A lease's bindings: references and metadata, never values",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
)
async def list_lease_bindings(
    lease_id: str, caller: CallerDep, service: ServiceDep, session: SessionDep
) -> BindingListResponse:
    """What credentials this lease produced, described but not disclosed.

    The query names its columns one by one instead of loading whole rows, so the sealed
    envelope is never read out of the database at all. That is not caution for its own
    sake -- :func:`assert_handlers_never_decrypt` refuses to let this module mention the
    sealed column, and selecting the row wholesale is how a future edit would end up with
    the ciphertext in memory next to a serialiser.

    Revoked bindings are included. "This credential was resolved four times before the
    resource was destroyed" is exactly the question an incident asks.
    """
    # Through the service first: it is the authority on whether this caller may see the
    # lease at all, and everything below assumes that question has been answered.
    view = await _call(service.get_lease(lease_id, caller))

    rows = await session.execute(
        select(
            Binding.id,
            Binding.reference,
            Binding.output_names,
            Binding.created_at,
            Binding.last_accessed_at,
            Binding.access_count,
            Binding.revoked_at,
        )
        .where(Binding.lease_id == view.id)
        .order_by(Binding.created_at.desc())
    )
    lease_is_usable = view.state in USABLE_STATES
    return BindingListResponse(
        lease_id=view.id,
        items=[
            BindingResponse(
                lease_id=view.id,
                reference=row.reference,
                output_names=sorted(row.output_names or []),
                created_at=aware(row.created_at),
                last_accessed_at=(
                    aware(row.last_accessed_at) if row.last_accessed_at is not None else None
                ),
                access_count=row.access_count,
                revoked_at=aware(row.revoked_at) if row.revoked_at is not None else None,
                usable=row.revoked_at is None and lease_is_usable,
                how_to_use=USAGE_HINT.format(lease_id=view.id),
            )
            for row in rows.all()
        ],
    )


@router.get(
    "/leases/{lease_id}/audit",
    tags=["leases"],
    summary="Everything that happened to one lease",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_lease_audit(
    lease_id: str,
    caller: CallerDep,
    service: ServiceDep,
    session: SessionDep,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
) -> AuditTrailResponse:
    """Oldest first, because it is a story and stories start at the beginning.

    Ordered by ``id`` after ``at`` so that two events written in the same instant -- a
    transition and the rollback note beside it -- keep a stable order between pages.
    """
    view = await _call(service.get_lease(lease_id, caller))
    rows = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.lease_id == view.id)
        .order_by(AuditEvent.at.asc(), AuditEvent.id.asc())
        .limit(limit + 1)
        .offset(offset)
    )
    items, has_more, _ = _page_bounds(list(rows.scalars().all()), limit, offset)
    return AuditTrailResponse(
        lease_id=view.id,
        items=[AuditEventResponse.from_event(event) for event in items],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


# --------------------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------------------


@router.get(
    "/approvals",
    tags=["approvals"],
    summary="The operator's approval inbox",
    responses=_AUTH_RESPONSES,
)
async def list_pending_approvals(
    caller: OperatorDep,
    session: SessionDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> ApprovalQueueResponse:
    """Undecided approval requests, oldest first.

    Oldest first because this is a queue and the interesting end is the one about to time
    out. An unanswered request is rejected by the ticker once its deadline passes -- with
    a reason that says nobody answered rather than a generic denial -- so a queue nobody
    reads quietly turns into a pile of rejections.

    Operator-only, which is also why this can be one joined query: there is no visibility
    predicate to apply, so there is no second copy of one to get wrong.
    """
    del caller
    now = utcnow()
    rows = await session.execute(
        select(Approval, Lease)
        .join(Lease, Lease.id == Approval.lease_id)
        .where(
            Approval.decided_at.is_(None),
            Lease.state == LeaseState.AWAITING_APPROVAL.value,
        )
        .order_by(Approval.requested_at.asc())
        .limit(limit + 1)
        .offset(offset)
    )
    pairs, has_more, _ = _page_bounds(list(rows.all()), limit, offset)
    items: list[PendingApprovalResponse] = []
    for approval, lease in pairs:
        requested_at = aware(approval.requested_at)
        deadline = aware(approval.deadline_at) if approval.deadline_at is not None else None
        items.append(
            PendingApprovalResponse(
                lease_id=lease.id,
                golden_path=lease.golden_path_id,
                provider=lease.provider,
                state=lease.lease_state,
                requester=lease.requester,
                on_behalf_of=lease.on_behalf_of,
                inputs=dict(lease.inputs or {}),
                ttl_seconds=lease.ttl_seconds,
                estimated_hourly_usd=lease.estimated_hourly_usd,
                reason=approval.reason,
                allowed_approvers=list(approval.allowed_approvers or []),
                policy_reason=lease.policy_reason,
                requested_at=requested_at,
                deadline_at=deadline,
                # Negative once the window has closed but the ticker has not swept it yet.
                # Clamping to zero would tell an approver they still have time to act on
                # something that can no longer be approved.
                seconds_until_deadline=(
                    int((deadline - now).total_seconds()) if deadline is not None else None
                ),
                waiting_seconds=int((now - requested_at).total_seconds()),
            )
        )
    return ApprovalQueueResponse(items=items, limit=limit, offset=offset, has_more=has_more)


@router.post(
    "/approvals/{lease_id}/approve",
    tags=["approvals"],
    summary="Grant an approval and queue the lease for provisioning",
    responses={
        **_AUTH_RESPONSES,
        **_NOT_FOUND,
        409: {"model": ErrorEnvelope, "description": "Already decided, or the window closed."},
    },
)
async def approve_lease(
    lease_id: str, body: ApproveRequestBody, caller: OperatorDep, service: ServiceDep
) -> LeaseResponse:
    """Approve.

    Operator-tier at the door and refused again in the service for any agent caller, on
    caller *kind* rather than on a permission string, so that no token configuration can
    produce an approving agent by accident. An approval gate the gated thing can open is
    not a gate.

    A request whose rule named specific approvers still needs one of them; holding an
    operator token is not the same as being on the list.
    """
    return LeaseResponse.from_view(await _call(service.approve(lease_id, caller, note=body.note)))


@router.post(
    "/approvals/{lease_id}/reject",
    tags=["approvals"],
    summary="Decline an approval",
    responses={
        **_AUTH_RESPONSES,
        **_NOT_FOUND,
        409: {"model": ErrorEnvelope, "description": "Already decided."},
    },
)
async def reject_lease(
    lease_id: str, body: RejectRequestBody, caller: OperatorDep, service: ServiceDep
) -> LeaseResponse:
    """Reject, with a reason that is shown verbatim to whoever asked.

    The reason is mandatory because the requester is usually an agent, and an agent told
    only "denied" tries again with a small variation until something works.
    """
    return LeaseResponse.from_view(
        await _call(service.reject(lease_id, caller, reason=body.reason))
    )


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


@router.get(
    "/reconcile/runs",
    tags=["reconcile"],
    summary="Reconcile history, newest first",
    responses=_AUTH_RESPONSES,
)
async def list_reconcile_runs(
    caller: OperatorDep,
    session: SessionDep,
    provider: Annotated[str | None, Query(description="Filter to one provider.")] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> ReconcileRunPage:
    """The counters, one row per provider per pass.

    Only providers that were actually checked ever get a row -- see
    :meth:`bailment.engine.reconciler.Reconciler._record_run` -- so an empty history for a
    provider means it was never swept, not that it was clean. The findings themselves live
    on the individual run.
    """
    del caller
    query = select(ReconcileRun).order_by(ReconcileRun.started_at.desc())
    if provider:
        query = query.where(ReconcileRun.provider == provider)
    rows = await session.execute(query.limit(limit + 1).offset(offset))
    items, has_more, _ = _page_bounds(list(rows.scalars().all()), limit, offset)
    return ReconcileRunPage(
        items=[ReconcileRunResponse.from_row(run) for run in items],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get(
    "/reconcile/runs/{run_id}",
    tags=["reconcile"],
    summary="One reconcile run, with the orphans and drift it found",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
)
async def get_reconcile_run(
    run_id: str, caller: OperatorDep, session: SessionDep
) -> ReconcileRunDetailResponse:
    del caller
    run = await session.get(ReconcileRun, run_id)
    if run is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "run_not_found", f"no reconcile run with id {run_id!r}"
        )
    return ReconcileRunDetailResponse.from_row_detail(run)


@router.post(
    "/reconcile",
    tags=["reconcile"],
    summary="Compare provider reality against the lease table, now",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND},
)
async def trigger_reconcile(
    caller: OperatorDep,
    sessionmaker: SessionmakerDep,
    registry: RegistryDep,
    settings: SettingsDep,
    provider: Annotated[
        str | None, Query(description="Sweep one provider instead of all of them.")
    ] = None,
) -> ReconcileTriggerResponse:
    """Run one pass and return what it found.

    **This can never destroy anything.** Orphan destruction needs two switches -- the
    global ``reconcile_auto_destroy_orphans`` *and* the provider's name in the
    reconciler's allowlist -- and the allowlist passed here is empty, unconditionally, no
    matter how the deployment is configured. A tool that deletes cloud resources because
    somebody was persuaded to click a button is a tool nobody installs twice; the armed
    path is ``bailment reconcile --destroy``, at a terminal, with a confirmation that
    lists the resources by name.

    Scoping to one provider builds a registry holding only that provider rather than
    passing a filter, because :meth:`~bailment.engine.reconciler.Reconciler.run` walks the
    registry it is given and a smaller registry cannot accidentally sweep something the
    operator did not name.

    The pass runs inline, so this request takes as long as the provider calls do. That is
    deliberate: a fire-and-forget trigger would answer 202 and leave the operator with no
    way to tell a clean sweep from one that never happened.
    """
    scoped = registry
    if provider is not None:
        if provider not in registry:
            raise http_error(
                status.HTTP_404_NOT_FOUND,
                "unknown_provider",
                f"no provider named {provider!r}; registered providers are "
                f"{', '.join(registry.names()) or '<none>'}",
            )
        scoped = ProviderRegistry()
        scoped.register(registry.get(provider))

    reconciler = Reconciler(
        sessionmaker,
        registry=scoped,
        settings=settings,
        destroy_orphans_for=(),  # Never armed from HTTP. See the docstring above.
    )
    summary = await reconciler.run()
    log.info(
        "reconcile triggered from the API",
        actor=caller.principal,
        provider=provider,
        orphans_found=summary.orphans_found,
        drift_found=summary.drift_found,
        clean=summary.clean,
    )
    return ReconcileTriggerResponse.from_summary(summary)


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------


@router.get(
    "/providers",
    tags=["providers"],
    summary="Which providers can actually be called",
    responses=_AUTH_RESPONSES,
)
async def list_providers(
    caller: OperatorDep,
    registry: RegistryDep,
    catalog: CatalogDep,
    settings: SettingsDep,
) -> ProviderListResponse:
    """Every registered provider, configured or not.

    Unconfigured providers are listed rather than hidden. An installation with no
    Cloudflare token should say "cloudflare: BAILMENT_CLOUDFLARE_API_TOKEN is not set",
    not behave as though Cloudflare does not exist -- and the reconciler needs the same
    distinction, because "this provider has no orphans" and "this provider was never
    asked" look identical in any dashboard that only lists what is working.
    """
    del caller
    return ProviderListResponse(
        items=[
            ProviderStatusResponse.from_status(
                report,
                missing_settings=settings.missing_provider_credentials(report.name),
                golden_paths=[path.id for path in catalog.by_provider(report.name)],
            )
            for report in registry.report()
        ]
    )


# --------------------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------------------


def _visible_to(caller: Caller) -> ColumnElement[bool] | None:
    """The rows this caller may be counted over. ``None`` means every row.

    **This must agree with** :meth:`bailment.engine.service.LeaseService.list_leases`,
    which is the authority. It exists as a second copy only because ``/stats`` is an
    aggregate and there is no way to sum a column through a method that returns views.
    Nothing else in this module may reimplement it: the list endpoints get their scoping
    from the service, and the two lease-scoped read endpoints ask the service to load the
    lease before they touch a table.
    """
    if caller.may_see_everything:
        return None
    return or_(Lease.requester == caller.principal, Lease.on_behalf_of == caller.principal)


@router.get(
    "/stats",
    tags=["stats"],
    summary="The dashboard header",
    responses=_AUTH_RESPONSES,
)
async def get_stats(
    caller: CallerDep,
    session: SessionDep,
    within: Annotated[
        int,
        Query(ge=60, le=86_400, description="Window for 'expiring soon', in seconds."),
    ] = 3600,
) -> StatsResponse:
    """Counts and estimated spend, scoped to what this caller may see.

    The spend figure sums every lease in a state where a real resource is believed to
    exist -- which includes ``ORPHANED`` and ``UNKNOWN``. Excluding those would produce a
    number that agrees with what bailment intended rather than with what is running, and
    the gap between those two is the thing this project exists to make visible.
    """
    now = utcnow()
    visible = _visible_to(caller)

    grouped_query = select(
        Lease.state,
        func.count(),
        func.coalesce(func.sum(Lease.estimated_hourly_usd), 0.0),
    ).group_by(Lease.state)
    if visible is not None:
        grouped_query = grouped_query.where(visible)
    grouped = await session.execute(grouped_query)

    live_values = {s.value for s in LIVE_STATES}
    by_state: dict[str, int] = {}
    hourly = 0.0
    for state, count, spend in grouped.all():
        by_state[str(state)] = int(count)
        if state in live_values:
            hourly += float(spend or 0.0)

    expiring_query = (
        select(func.count())
        .select_from(Lease)
        .where(
            Lease.state.in_([s.value for s in USABLE_STATES]),
            Lease.expires_at.is_not(None),
            # Compared in SQL, where the bound parameter is rendered with the same
            # convention the column was written with. See engine.service.aware.
            Lease.expires_at <= now + timedelta(seconds=within),
        )
    )
    if visible is not None:
        expiring_query = expiring_query.where(visible)
    expiring = await session.execute(expiring_query)

    last_run: ReconcileRun | None = None
    if caller.may_see_everything:
        rows = await session.execute(
            select(ReconcileRun).order_by(ReconcileRun.started_at.desc()).limit(1)
        )
        last_run = rows.scalars().first()

    hourly = round(hourly, 6)
    return StatsResponse(
        scope="all" if caller.may_see_everything else "own",
        at=now,
        active_leases=sum(by_state.get(s.value, 0) for s in USABLE_STATES),
        expiring_soon=int(expiring.scalar_one()),
        expiring_within_seconds=within,
        awaiting_approval=by_state.get(LeaseState.AWAITING_APPROVAL.value, 0),
        orphans_outstanding=by_state.get(LeaseState.ORPHANED.value, 0),
        live_leases=sum(by_state.get(s.value, 0) for s in LIVE_STATES),
        total_leases=sum(by_state.values()),
        estimated_hourly_usd=hourly,
        estimated_daily_usd=round(hourly * 24, 4),
        estimated_monthly_usd=round(hourly * HOURS_PER_MONTH, 2),
        by_state=by_state,
        last_reconcile=ReconcileRunResponse.from_row(last_run) if last_run is not None else None,
    )


# --------------------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------------------

#: Names that lead to a decrypted credential. A handler that mentions any of them is
#: either resolving a binding, building the box that opens one, or reading the sealed
#: column -- and none of those may happen in an HTTP request.
#:
#: This is not a style rule. The API's operator tier maps to
#: :attr:`~bailment.engine.service.CallerKind.OPERATOR`, which the service *permits* to
#: decrypt, so nothing below this module would refuse a handler that asked. This is the
#: thing that refuses.
DECRYPTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "resolve_binding",
        "secret_box",
        "SecretBox",
        "ciphertext",
        "decryption_keys",
        "require_encryption_key",
    }
)


def _code_names(code: CodeType, into: set[str]) -> None:
    """Collect every global and attribute name a code object refers to.

    Recurses through ``co_consts`` because a comprehension, a lambda or a nested function
    compiles to its own code object, and a name used only inside one would otherwise be
    invisible to the check.
    """
    into.update(code.co_names)
    for const in code.co_consts:
        if isinstance(const, CodeType):
            _code_names(const, into)


def _own_code(module: ModuleType) -> Iterator[CodeType]:
    """Code objects for the functions and methods *defined in* ``module``.

    Imported callables are skipped by comparing ``__module__``. Without that, the check
    would scan the internals of everything this module imports and report a violation for
    somebody else's perfectly correct code.
    """
    for value in vars(module).values():
        code = getattr(value, "__code__", None)
        if isinstance(code, CodeType) and getattr(value, "__module__", None) == module.__name__:
            yield code
        if isinstance(value, type) and value.__module__ == module.__name__:
            for member in vars(value).values():
                member_code = getattr(member, "__code__", None)
                if isinstance(member_code, CodeType):
                    yield member_code


def referenced_names(module: ModuleType) -> set[str]:
    """Every name the code defined in ``module`` reads. Exposed so tests can inspect it."""
    names: set[str] = set()
    for code in _own_code(module):
        _code_names(code, names)
    return names


#: What the check covers by default: the handlers *and* the dependencies that feed them.
#: A dependency can reach plaintext exactly as easily as a handler can, and it would be a
#: stranger place to look for it.
GUARDED_MODULES: Final[tuple[str, ...]] = ("bailment.api.routes", "bailment.api.deps")


def assert_handlers_never_decrypt(module: ModuleType | None = None) -> None:
    """Refuse to serve if any handler can reach a plaintext credential.

    Runs at import time, so this is a startup failure rather than a code review somebody
    might not do. It is a structural check rather than a naming convention: to return a
    secret, a handler has to call
    :meth:`~bailment.engine.service.LeaseService.resolve_binding`, construct a
    :class:`~bailment.secrets.SecretBox`, or read the sealed column off a binding row, and
    all three leave their name in the compiled code -- including inside a comprehension or
    a nested function, which is why :func:`_code_names` recurses.

    Pass a module to check that one; the default is :data:`GUARDED_MODULES`.

    The practical consequence, for whoever edits this file next: query binding metadata by
    naming its columns, never by loading whole rows. That is the shape this check enforces
    and it is the right shape anyway -- the sealed envelope never leaves the database.
    """
    targets = (
        [module]
        if module is not None
        else [sys.modules[name] for name in GUARDED_MODULES if name in sys.modules]
    )
    offenders = sorted(
        f"{target.__name__} refers to {name}"
        for target in targets
        for name in referenced_names(target) & DECRYPTION_NAMES
    )
    if offenders:
        raise RuntimeError(
            "; ".join(offenders) + ". Those names lead to a decrypted credential, and no "
            "part of the REST API may resolve a binding: bailment hands out capabilities, "
            "not credentials, and a value in an HTTP response body is a value in the proxy "
            "log, the browser and every tool that records response payloads. Consumers use "
            "'bailment exec <lease-id> -- <command>', which injects into one process and "
            "writes the value nowhere else."
        )


assert_handlers_never_decrypt()
