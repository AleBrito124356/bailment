"""Open Service Broker API v2.16, so that things which already speak OSB need no new client.

The Open Service Broker API is the interoperability standard nobody talks about any more
and everybody still ships. Its Kubernetes implementation -- the service-catalog project --
was archived in 2022, which is why people assume the spec died with it. It did not: the
spec itself is alive at v2.15 and above, Cloud Foundry drives it in production every day,
several CI systems and internal developer portals speak it, and any team that ever wrote
a broker has a client sitting in a repository somewhere. Implementing it costs bailment
one module and buys every one of those callers for free. That is the whole argument for
this file.

The mapping is direct, because the golden path already contains everything a service
needs::

    GoldenPath              -> service, with exactly one plan
    GoldenPath.inputs       -> plan.schemas.service_instance.create.parameters
    service instance        -> one lease
    service binding         -> that lease's binding

Service and plan ids are UUIDv5 values derived from the golden path id under a fixed
namespace, so they are stable across restarts, stable across deployments, and stable
across a catalog reload. A broker that hands out fresh ids on restart breaks every
platform that stored them, which is most of them.

----

**The credential divergence, which is the important paragraph in this file.**

Everywhere else in bailment, a secret value is unreachable: the REST API returns a
reference, the MCP tools return a reference, the dashboard renders output *names*, and
:meth:`bailment.engine.service.LeaseService.resolve_binding` is the only function that
opens an envelope. OSB does not permit that. A service binding response carries a
``credentials`` object by definition -- it is the entire reason the endpoint exists, and a
broker that returned a reference instead would be a broker no platform can use.

So this surface diverges, deliberately and in exactly one place: ``PUT
/v2/service_instances/{id}/service_bindings/{id}`` returns credential values, and it
requires an **operator** token to do it. An ordinary API token gets 403 with a message
saying so. There is no configuration that lets an agent through: MCP callers are
constructed with :attr:`~bailment.engine.service.CallerKind.AGENT`, which
``resolve_binding`` refuses regardless of which HTTP path reached it.

The rule, stated once: **agents use MCP, humans and CI systems use OSB.**

----

**Three places this broker is not literally conformant, and why.**

*Bindings are aliases, not resources.* A lease has one binding, sealed once when the
resource was created. OSB models bindings as independently creatable, so a second
``PUT`` with a different binding id would be expected to mint a second credential.
bailment cannot: the provider issued one credential and inventing a second would mean
holding a credential the reconciler cannot account for. Every OSB binding id on one
instance therefore resolves to the same underlying binding, and a repeated ``PUT``
answers ``201`` rather than distinguishing first from repeat, because nothing in the
lease records which OSB binding ids have been seen.

*Unbind does not revoke.* A bailment credential's lifetime is its lease's -- that is the
promise the project is built on -- so ``DELETE`` on a binding is acknowledged and
recorded, and the credential keeps working until the lease ends. To end a credential,
delete the instance, which revokes the lease and destroys the resource. Telling a
platform "unbound" and then leaving the credential live would be worse; saying so here is
the honest option.

*Provisioning is always asynchronous.* Every request needs ``accepts_incomplete=true``.
bailment never calls a provider on the request thread -- the write-ahead name has to be
committed first, and a human may have to approve the request at all -- so there is no
code path that could answer ``201 Created`` truthfully.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bailment.catalog.loader import Catalog
from bailment.catalog.schema import GoldenPath
from bailment.config import Settings
from bailment.db import get_session
from bailment.engine.service import (
    Caller,
    CallerKind,
    ConflictingState,
    InvalidRequest,
    LeaseNotFound,
    LeaseService,
    LeaseView,
    NotAuthorized,
    PathDisabled,
    ProviderUnavailable,
    ProvisionRequest,
    RenewalRefused,
    SecretAccessDenied,
    ServiceError,
    UnknownPath,
)
from bailment.logging import get_logger
from bailment.models import Lease
from bailment.providers.registry import ProviderRegistry
from bailment.states import TERMINAL_STATES, LeaseState

__all__ = [
    "OSB_API_VERSION",
    "build_osb_router",
    "plan_id_for",
    "service_id_for",
]

log = get_logger("bailment.osb")

#: The version this broker implements and advertises.
OSB_API_VERSION: Final = "2.16"

#: Required on every request. A platform that omits it is a platform whose expectations we
#: cannot check, and 412 is what the spec says to answer.
VERSION_HEADER: Final = "X-Broker-API-Version"

#: Optional. Carries the end user the platform is acting for, base64-encoded JSON after a
#: platform name. Recorded as ``on_behalf_of`` so the audit trail can tell "which CI
#: system" apart from "whose pipeline".
IDENTITY_HEADER: Final = "X-Broker-API-Originating-Identity"

#: How an OSB instance id is stored on the lease it created. The instance id is generated
#: by the platform and is unique per instance, which is precisely the contract
#: ``Lease.idempotency_key`` wants: a repeated PUT returns the same lease rather than
#: provisioning a second resource.
INSTANCE_KEY_PREFIX: Final = "osb:"

#: Fixed namespace for the UUIDv5 service and plan ids. Never change it: platforms store
#: the ids they were given, and a new namespace would orphan every registered service.
_ID_NAMESPACE: Final = uuid.UUID("2f7d0e79-0f5f-4b0e-9a5b-0f1f8a2c6d31")

#: What we tell platforms about how long polling may reasonably take. A day, because a
#: path whose policy requires approval genuinely can sit in ``awaiting_approval`` until a
#: human gets to it, and a platform that gives up after ten minutes reports a failure for
#: a request that is merely queued behind a person.
_MAX_POLLING_SECONDS: Final = 86_400

#: States where the provision operation has not resolved yet.
_PROVISION_PENDING: Final = frozenset(
    {LeaseState.PENDING, LeaseState.AWAITING_APPROVAL, LeaseState.PROVISIONING}
)

#: States that mean the resource exists and the instance is usable.
_PROVISION_SUCCEEDED: Final = frozenset({LeaseState.ACTIVE, LeaseState.EXPIRING})


# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------


def service_id_for(golden_path_id: str) -> str:
    """The OSB service id for a golden path. Deterministic; see the module docstring."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"bailment:service:{golden_path_id}"))


def plan_id_for(golden_path_id: str) -> str:
    """The OSB plan id for a golden path's single plan."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"bailment:plan:{golden_path_id}"))


def _resolve_service(catalog: Catalog, service_id: str) -> GoldenPath | None:
    """Find the golden path a ``service_id`` refers to.

    Accepts the derived UUID *or* the bare golden path id. Platforms send back exactly
    what the catalog gave them, so the UUID is the real path; the bare id is accepted
    because it is what a human writes when driving this endpoint with curl, and refusing
    it buys nothing.
    """
    given = service_id.strip()
    if not given:
        return None
    direct = catalog.find(given)
    if direct is not None:
        return direct
    for path in catalog.all():
        if service_id_for(path.id) == given:
            return path
    return None


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class _Fault(Exception):
    """An OSB-shaped failure.

    Every error this router emits is one of these, so there is exactly one place that
    knows what an OSB error body looks like. The spec's body is ``{"error": ...,
    "description": ...}`` where ``error`` is a single camel-case word from a short
    documented list; ``description`` is prose shown to whoever ran the command.
    """

    def __init__(self, status: int, error: str, description: str) -> None:
        self.status = status
        self.error = error
        self.description = description
        super().__init__(f"{error}: {description}")

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"error": self.error, "description": self.description},
        )


#: Service-layer failures, mapped to the OSB status codes and error words. Kept as a table
#: rather than a chain of ``isinstance`` branches so that adding a service error without
#: deciding what it means over OSB is a visible omission rather than a silent 500.
_SERVICE_FAULTS: Final[dict[type[ServiceError], tuple[int, str]]] = {
    UnknownPath: (400, "ServiceIdMissing"),
    PathDisabled: (400, "ServiceIdMissing"),
    InvalidRequest: (400, "InvalidParameters"),
    LeaseNotFound: (404, "InstanceNotFound"),
    SecretAccessDenied: (403, "Forbidden"),
    NotAuthorized: (403, "Forbidden"),
    RenewalRefused: (422, "UpdateRefused"),
    ConflictingState: (409, "ConcurrencyError"),
    ProviderUnavailable: (503, "ProviderUnavailable"),
}


def _as_fault(exc: Exception) -> _Fault:
    if isinstance(exc, _Fault):
        return exc
    for kind, (status, error) in _SERVICE_FAULTS.items():
        if isinstance(exc, kind):
            return _Fault(status, error, str(exc))
    # A ServiceError nobody mapped. 500 rather than a guess: an unmapped failure is a gap
    # in this table, and dressing it up as a 4xx would tell the platform the request was
    # at fault when the broker is.
    log.error("unmapped service error on the OSB surface", error=str(exc))
    return _Fault(500, "BrokerError", str(exc))


# --------------------------------------------------------------------------------------
# Request plumbing
# --------------------------------------------------------------------------------------


def _catalog(request: Request) -> Catalog:
    """The catalog this app was assembled with.

    Read off ``app.state`` rather than captured in a closure so that a catalog reload
    swaps the object and every subsequent request sees the new one. The isinstance check
    is not defensive programming for its own sake -- ``app.state`` is untyped, and a
    missing attribute would otherwise surface as ``AttributeError`` inside a handler.
    """
    value = getattr(request.app.state, "catalog", None)
    if not isinstance(value, Catalog):
        raise _Fault(
            503,
            "BrokerUnavailable",
            "this broker has no catalog loaded; bailment.main.create_app puts one on "
            "app.state.catalog and something has assembled the app without it",
        )
    return value


def _registry(request: Request) -> ProviderRegistry:
    value = getattr(request.app.state, "registry", None)
    if not isinstance(value, ProviderRegistry):
        raise _Fault(503, "BrokerUnavailable", "this broker has no provider registry on app.state")
    return value


def _settings(request: Request) -> Settings:
    value = getattr(request.app.state, "settings", None)
    if not isinstance(value, Settings):
        raise _Fault(503, "BrokerUnavailable", "this broker has no settings on app.state")
    return value


def _service(request: Request, session: AsyncSession) -> LeaseService:
    return LeaseService(
        session,
        catalog=_catalog(request),
        registry=_registry(request),
        settings=_settings(request),
    )


def _require_version(request: Request) -> None:
    """Enforce ``X-Broker-API-Version``.

    The spec makes this header mandatory and 412 the answer when it is absent or names a
    major version the broker does not implement. A minor version above ours is accepted:
    the platform is expected to ignore fields it did not ask for, and refusing a client
    for being newer breaks upgrades in the wrong direction.
    """
    raw = request.headers.get(VERSION_HEADER)
    if not raw:
        raise _Fault(
            412,
            "PreconditionFailed",
            f"{VERSION_HEADER} is required on every request to this broker. Send "
            f"'{VERSION_HEADER}: {OSB_API_VERSION}'.",
        )
    major, _, minor = raw.strip().partition(".")
    if major != "2" or not minor.isdigit():
        raise _Fault(
            412,
            "PreconditionFailed",
            f"this broker implements Open Service Broker API {OSB_API_VERSION}; "
            f"{raw!r} is not a version it can serve.",
        )


def _presented_credential(header: str) -> str | None:
    """Pull the token out of an ``Authorization`` header.

    Both schemes, because OSB platforms overwhelmingly use HTTP Basic (the broker
    registration form has a username and a password field) while everything else that
    talks to bailment uses a bearer token. For Basic the password half is the token and
    the username is ignored: bailment's identities come from the token itself, so a
    username is decoration, and treating it as meaningful would create a second, weaker
    way to claim a principal.
    """
    scheme, _, rest = header.partition(" ")
    value = rest.strip()
    if not value:
        return None
    lowered = scheme.strip().lower()
    if lowered == "bearer":
        return value
    if lowered == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        _, separator, password = decoded.partition(":")
        return password if separator else None
    return None


def _originating_identity(request: Request) -> str | None:
    """The end user the platform says it is acting for, or ``None``.

    Format is ``<platform> <base64 of a JSON object>``. Malformed values are dropped
    rather than rejected: this is an audit-trail nicety, and failing a provisioning
    request because a platform's base64 padding was wrong would be the wrong trade.
    """
    raw = request.headers.get(IDENTITY_HEADER)
    if not raw:
        return None
    platform, _, encoded = raw.strip().partition(" ")
    if not encoded:
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("username", "user_name", "user_id", "uid", "sub", "email"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"{platform}:{value.strip()}"[:255]
    return None


def _authenticate(request: Request) -> Caller:
    """Resolve the caller, or refuse.

    OSB callers are never agents. An admin token becomes an ``OPERATOR``, which is what
    ``resolve_binding`` requires and therefore what a service binding needs; an ordinary
    token becomes a ``HUMAN``, which can provision and delete instances but cannot read a
    credential value. The kind is decided here, at the surface, which is the whole point
    of :class:`~bailment.engine.service.CallerKind` being an argument rather than a
    convention.
    """
    settings = _settings(request)
    presented = _presented_credential(request.headers.get("authorization", ""))
    identity = settings.lookup_token(presented) if presented else None
    on_behalf_of = _originating_identity(request)

    if identity is None:
        if presented is None and settings.allow_anonymous:
            return Caller("anonymous", CallerKind.HUMAN, on_behalf_of=on_behalf_of)
        raise _Fault(
            401,
            "Unauthorized",
            "this broker needs credentials. Register it with the token in the password "
            "field of HTTP Basic auth, or send 'Authorization: Bearer <token>'.",
        )
    return Caller(
        identity.principal,
        CallerKind.OPERATOR if identity.admin else CallerKind.HUMAN,
        on_behalf_of=on_behalf_of,
    )


async def _read_body(request: Request) -> dict[str, Any]:
    """Parse the request body, in OSB's error envelope rather than FastAPI's.

    Done by hand instead of with a pydantic model on the signature for one reason: a
    validation failure from FastAPI produces ``{"detail": [...]}``, which is not an OSB
    error body, and a platform parsing it gets a blank error message at the moment
    somebody is trying to work out why their provisioning failed.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise _Fault(400, "MalformedRequest", f"request body is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise _Fault(
            400, "MalformedRequest", "request body must be a JSON object, per the OSB spec"
        )
    return parsed


def _require_async(request: Request) -> None:
    """Every mutating OSB operation here is asynchronous. See the module docstring."""
    value = request.query_params.get("accepts_incomplete", "").strip().lower()
    if value != "true":
        raise _Fault(
            422,
            "AsyncRequired",
            "This broker only supports asynchronous provisioning and deprovisioning: "
            "the lease row that names the resource has to be committed before any "
            "provider is called, and a request may need a human to approve it. Retry "
            "with accepts_incomplete=true and poll last_operation.",
        )


# --------------------------------------------------------------------------------------
# Catalog rendering
# --------------------------------------------------------------------------------------


def _plan(path: GoldenPath) -> dict[str, Any]:
    """The single plan a golden path offers.

    One plan, not one per TTL bracket. TTL is a parameter with a default and a documented
    ceiling, and encoding it as plans would mean a catalog change every time somebody
    wanted three hours instead of four.
    """
    costs: list[dict[str, Any]] = []
    if path.cost.estimated_hourly_usd:
        costs.append({"amount": {"usd": path.cost.estimated_hourly_usd}, "unit": "HOURLY"})
    if path.cost.estimated_monthly_usd:
        costs.append({"amount": {"usd": path.cost.estimated_monthly_usd}, "unit": "MONTHLY"})

    bullets = [
        f"Leases run for {path.lease.default_ttl} by default, "
        f"{path.lease.max_ttl} at most, then the resource is destroyed.",
        (
            f"Renewable up to {path.lease.max_renewals} time(s) before it expires."
            if path.lease.renewable
            else "Not renewable: ask for the duration you need up front."
        ),
        "Credentials are returned by a service binding and require an operator token.",
    ]
    if path.cost.note:
        bullets.append(path.cost.note)

    return {
        "id": plan_id_for(path.id),
        "name": "standard",
        "description": (
            f"{path.lease.default_ttl} lease by default, up to {path.lease.max_ttl}. "
            f"The resource is destroyed when the lease ends."
        ),
        "free": not path.cost.estimated_hourly_usd and not path.cost.estimated_monthly_usd,
        # A path that declares no outputs produces no binding, so saying it is bindable
        # would advertise an endpoint that can only ever answer 422.
        "bindable": bool(path.outputs),
        "maximum_polling_duration": _MAX_POLLING_SECONDS,
        "metadata": {
            "displayName": path.name,
            "bullets": bullets,
            "costs": costs,
            "bailment": {
                "default_ttl": str(path.lease.default_ttl),
                "max_ttl": str(path.lease.max_ttl),
                "warn_before": str(path.lease.warn_before),
                "renewable": path.lease.renewable,
                "max_renewals": path.lease.max_renewals,
            },
        },
        "schemas": {
            # The golden path's own input schema, verbatim, plus the universal ttl
            # argument. Same object the MCP tool advertises and the dashboard form
            # renders: one definition, three surfaces, no drift.
            "service_instance": {
                "create": {"parameters": path.input_schema_with_lease()},
                "update": {"parameters": _update_schema(path)},
            },
            "service_binding": {
                "create": {
                    "parameters": {
                        "$schema": "http://json-schema.org/draft-04/schema#",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    }
                }
            },
        },
    }


def _update_schema(path: GoldenPath) -> dict[str, Any]:
    """What an OSB update may change: the TTL, and nothing else.

    Changing an input would mean destroying and recreating the resource behind a running
    lease, which is not an update, it is a new instance with the old one's name.
    """
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ttl": {
                "type": "string",
                "description": (
                    f"Renew the lease for this much longer, measured from now, e.g. '2h'. "
                    f"Capped at {path.lease.max_ttl}"
                    + (
                        f"; this plan allows {path.lease.max_renewals} renewal(s)."
                        if path.lease.renewable
                        else ", but this plan is not renewable."
                    )
                ),
            }
        },
    }


def _service_entry(path: GoldenPath) -> dict[str, Any]:
    return {
        "id": service_id_for(path.id),
        # CLI-friendly and unique: the golden path id already has both properties, and
        # reusing it means the name a platform shows matches the name in the catalog file.
        "name": path.id,
        "description": _one_line(path.description),
        "bindable": bool(path.outputs),
        "instances_retrievable": False,
        "bindings_retrievable": False,
        "plan_updateable": False,
        "tags": list(path.tags),
        "metadata": {
            "displayName": path.name,
            "longDescription": path.description,
            "providerDisplayName": "bailment",
            "bailment": {
                "golden_path_id": path.id,
                "provider": path.provider,
                "mcp_tool": path.mcp_tool_name,
                "outputs": [
                    {"name": output.name, "secret": output.secret} for output in path.outputs
                ],
            },
        },
        "plans": [_plan(path)],
    }


def _one_line(text: str) -> str:
    """First paragraph, collapsed. Platform UIs show ``description`` in a table cell."""
    first = text.strip().split("\n\n", 1)[0]
    return " ".join(first.split())


# --------------------------------------------------------------------------------------
# Lease lookup
# --------------------------------------------------------------------------------------


async def _lease_id_for_instance(session: AsyncSession, instance_id: str) -> str | None:
    """Which lease an OSB instance id names, if any.

    Reads the id column and nothing else, then everything downstream goes through
    :class:`~bailment.engine.service.LeaseService`, which is what applies the visibility
    rules. The same split the CLI uses to expand a lease id prefix, and for the same
    reason: the service has no "find by idempotency key" method and inventing a second
    place that decides who may see a lease would be a great deal worse than one indexed
    read of a primary key.
    """
    result = await session.execute(
        select(Lease.id)
        .where(Lease.idempotency_key == INSTANCE_KEY_PREFIX + instance_id)
        .order_by(Lease.created_at.asc())
        .limit(1)
    )
    found = result.scalars().first()
    return str(found) if found is not None else None


async def _view_for_instance(
    service: LeaseService, session: AsyncSession, instance_id: str, caller: Caller
) -> LeaseView | None:
    """The lease behind an instance id, or ``None`` if there is none this caller may see.

    A lease that exists but belongs to somebody else returns ``None`` too, which is what
    turns into 404 or 410 depending on the endpoint. Confirming that an instance id is
    real to a caller who cannot read it is an enumeration oracle, and the service already
    made that decision for the REST surface.
    """
    lease_id = await _lease_id_for_instance(session, instance_id)
    if lease_id is None:
        return None
    try:
        return await service.get_lease(lease_id, caller)
    except LeaseNotFound:
        return None


def _dashboard_url(settings: Settings, lease_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/leases/{lease_id}"


def _parameters_conflict(view: LeaseView, submitted: Mapping[str, Any]) -> bool:
    """Whether a repeated PUT asked for something different from the first one.

    Only the keys the caller actually sent are compared. The stored inputs have been
    through schema validation, which fills in declared defaults, so comparing the two
    dicts wholesale would report a conflict for a caller that simply omitted an optional
    field it omitted the first time too.
    """
    for key, value in submitted.items():
        if key == "ttl":
            # TTL is not part of an instance's identity: it is clamped, it is renewable,
            # and refusing a retry because the caller asked for a different duration would
            # make the idempotency key useless for the one caller that needs it most.
            continue
        if view.inputs.get(key) != value:
            return True
    return False


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def build_osb_router() -> APIRouter:
    """The OSB endpoints. Mount at ``/v2``; every path below is relative to it."""
    router = APIRouter(tags=["open service broker"])

    @router.get(
        "/catalog",
        summary="OSB catalog: one service per enabled golden path",
        response_model=None,
    )
    async def catalog(request: Request) -> Response:
        try:
            _require_version(request)
            _authenticate(request)
            paths = _catalog(request).enabled()
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()
        return JSONResponse({"services": [_service_entry(path) for path in paths]})

    @router.put(
        "/service_instances/{instance_id}",
        summary="Provision an instance, which is to say take out a lease",
        response_model=None,
    )
    async def provision(
        instance_id: str,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        try:
            _require_version(request)
            caller = _authenticate(request)
            _require_async(request)
            body = await _read_body(request)
            service = _service(request, session)

            path = _require_path(_catalog(request), body)
            _require_plan(path, body)
            parameters = _parameters(body)

            existing = await _view_for_instance(service, session, instance_id, caller)
            if existing is not None:
                return _existing_instance(_settings(request), existing, path, parameters)
            if await _lease_id_for_instance(session, instance_id) is not None:
                # The id maps to a lease this caller cannot see. Conflict rather than 404:
                # the instance id really is taken, and a platform that retried with the
                # same id would keep failing for a reason nothing else would explain.
                raise _Fault(
                    409,
                    "Conflict",
                    f"service instance {instance_id} already exists and belongs to another "
                    f"principal.",
                )

            outcome = await service.request_provision(
                caller,
                ProvisionRequest(
                    golden_path_id=path.id,
                    inputs=parameters,
                    idempotency_key=INSTANCE_KEY_PREFIX + instance_id,
                ),
            )
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()

        view = outcome.lease
        log.info(
            "osb provision accepted",
            instance_id=instance_id,
            lease_id=view.id,
            golden_path=view.golden_path_id,
            state=view.state.value,
            requester=view.requester,
        )
        if view.state is LeaseState.REJECTED:
            return _Fault(
                400,
                "PolicyDenied",
                _denial_text(view),
            ).response()
        return JSONResponse(
            status_code=202,
            content={
                "operation": "provision",
                "dashboard_url": _dashboard_url(_settings(request), view.id),
            },
        )

    @router.patch(
        "/service_instances/{instance_id}",
        summary="Update an instance: renew its lease",
        response_model=None,
    )
    async def update(
        instance_id: str,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        try:
            _require_version(request)
            caller = _authenticate(request)
            body = await _read_body(request)
            service = _service(request, session)

            if "plan_id" in body and isinstance(body["plan_id"], str):
                view_for_plan = await _view_for_instance(service, session, instance_id, caller)
                if view_for_plan is not None:
                    path = _catalog(request).find(view_for_plan.golden_path_id)
                    if path is not None and body["plan_id"] != plan_id_for(path.id):
                        raise _Fault(
                            422,
                            "PlanChangeNotSupported",
                            "each bailment service has exactly one plan, so there is no "
                            "other plan to move to.",
                        )

            parameters = _parameters(body)
            unknown = sorted(set(parameters) - {"ttl"})
            if unknown:
                raise _Fault(
                    422,
                    "UpdateRefused",
                    f"only 'ttl' can be updated on a live instance; {', '.join(unknown)} "
                    f"would mean destroying the resource and creating a different one. "
                    f"Delete this instance and provision a new one.",
                )
            ttl = parameters.get("ttl")
            if ttl is None:
                raise _Fault(
                    422,
                    "UpdateRefused",
                    "nothing to update. The only supported update is a 'ttl' parameter, "
                    "such as {'parameters': {'ttl': '2h'}}, which renews the lease from now.",
                )
            if not isinstance(ttl, str):
                raise _Fault(422, "InvalidParameters", "ttl must be a duration string, e.g. '2h'")

            view = await _view_for_instance(service, session, instance_id, caller)
            if view is None:
                raise _Fault(
                    404, "InstanceNotFound", f"no service instance {instance_id} at this broker"
                )
            outcome = await service.renew(view.id, caller, ttl=ttl)
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()

        renewed = outcome.lease
        log.info(
            "osb instance renewed",
            instance_id=instance_id,
            lease_id=renewed.id,
            expires_at=renewed.expires_at.isoformat() if renewed.expires_at else None,
        )
        # Synchronous: renewal is arithmetic on a row, no provider call, nothing to poll.
        return JSONResponse(
            status_code=200,
            content={
                "dashboard_url": _dashboard_url(_settings(request), renewed.id),
                "metadata": _instance_metadata(renewed, outcome.notices),
            },
        )

    @router.delete(
        "/service_instances/{instance_id}",
        summary="Deprovision an instance, which is to say revoke its lease",
        response_model=None,
    )
    async def deprovision(
        instance_id: str,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        try:
            _require_version(request)
            caller = _authenticate(request)
            _require_async(request)
            service = _service(request, session)

            if not request.query_params.get("service_id"):
                raise _Fault(
                    400,
                    "ServiceIdMissing",
                    "service_id is a required query parameter on deprovision.",
                )
            view = await _view_for_instance(service, session, instance_id, caller)
            if view is None or view.state in TERMINAL_STATES:
                # 410 Gone is what the spec asks for when the instance does not exist, and
                # a lease that is released, failed or rejected has no resource behind it.
                return JSONResponse(status_code=410, content={})

            await service.revoke(
                view.id,
                caller,
                reason=(
                    f"deprovisioned over the Open Service Broker API by "
                    f"{caller.principal!r} (instance {instance_id})"
                ),
            )
            log.info("osb deprovision accepted", instance_id=instance_id, lease_id=view.id)
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()
        return JSONResponse(status_code=202, content={"operation": "deprovision"})

    @router.get(
        "/service_instances/{instance_id}/last_operation",
        summary="Poll the state of a provision or deprovision",
        response_model=None,
    )
    async def last_operation(
        instance_id: str,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        try:
            _require_version(request)
            caller = _authenticate(request)
            service = _service(request, session)
            operation = request.query_params.get("operation", "").strip().lower()
            view = await _view_for_instance(service, session, instance_id, caller)
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()

        if view is None:
            if operation == "deprovision":
                # The platform asked whether its delete finished and the lease is gone
                # entirely. Per the spec, that is 410 rather than a state report.
                return JSONResponse(status_code=410, content={})
            return _Fault(
                404, "InstanceNotFound", f"no service instance {instance_id} at this broker"
            ).response()

        if operation == "deprovision":
            state, description = _deprovision_progress(view)
            if state == "gone":
                return JSONResponse(status_code=410, content={})
        else:
            state, description = _provision_progress(view)
        return JSONResponse({"state": state, "description": description})

    @router.put(
        "/service_instances/{instance_id}/service_bindings/{binding_id}",
        summary="Bind: return the lease's credentials. Operator token required.",
        response_model=None,
    )
    async def bind(
        instance_id: str,
        binding_id: str,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        try:
            _require_version(request)
            caller = _authenticate(request)
            await _read_body(request)
            service = _service(request, session)

            if not caller.is_operator:
                # The divergence, gated. See the module docstring.
                raise _Fault(
                    403,
                    "Forbidden",
                    "a service binding returns credential values, so it needs an operator "
                    "token. An ordinary token can provision, poll and delete instances but "
                    "never reads a credential. If you are an AI agent, you are on the wrong "
                    "surface: use the MCP tools, which hand back a reference and let "
                    "'bailment exec' inject the value into the process that needs it.",
                )

            view = await _view_for_instance(service, session, instance_id, caller)
            if view is None:
                raise _Fault(
                    404, "InstanceNotFound", f"no service instance {instance_id} at this broker"
                )
            if not view.usable:
                raise _Fault(
                    422,
                    "ConcurrencyError",
                    f"instance {instance_id} is {view.state.value}, so there is no live "
                    f"credential to hand over. Poll last_operation until the provision "
                    f"succeeds; if it has already ended, provision a new instance.",
                )
            if view.binding_reference is None:
                raise _Fault(
                    422,
                    "RequiresApp",
                    f"the {view.golden_path_id!r} service produces no credentials, so it "
                    f"has nothing to bind. Its catalog entry says bindable: false.",
                )
            values = await service.resolve_binding(view.binding_reference, caller)
            log.info(
                "osb binding issued",
                instance_id=instance_id,
                binding_id=binding_id,
                lease_id=view.id,
                principal=caller.principal,
                output_names=sorted(values),
            )
            # `values` holds live credential material. It goes into the response body and
            # nowhere else -- not into a log line, not into the audit detail, not into an
            # exception message. The audit row was written by resolve_binding, which
            # records the output names and the access count and never the values.
            content: dict[str, Any] = {
                "credentials": values,
                "metadata": _instance_metadata(view, ()),
            }
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()
        return JSONResponse(status_code=201, content=content)

    @router.delete(
        "/service_instances/{instance_id}/service_bindings/{binding_id}",
        summary="Unbind. Acknowledged; the credential lives as long as the lease.",
        response_model=None,
    )
    async def unbind(
        instance_id: str,
        binding_id: str,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        try:
            _require_version(request)
            caller = _authenticate(request)
            service = _service(request, session)
            if not request.query_params.get("service_id"):
                raise _Fault(
                    400, "ServiceIdMissing", "service_id is a required query parameter on unbind."
                )
            view = await _view_for_instance(service, session, instance_id, caller)
        except (_Fault, ServiceError) as exc:
            return _as_fault(exc).response()

        if view is None:
            return JSONResponse(status_code=410, content={})
        log.info(
            "osb unbind acknowledged",
            instance_id=instance_id,
            binding_id=binding_id,
            lease_id=view.id,
            note="credential remains valid until the lease ends",
        )
        return JSONResponse(status_code=200, content={})

    return router


# --------------------------------------------------------------------------------------
# Handler helpers
# --------------------------------------------------------------------------------------


def _require_path(catalog: Catalog, body: Mapping[str, Any]) -> GoldenPath:
    service_id = body.get("service_id")
    if not isinstance(service_id, str) or not service_id.strip():
        raise _Fault(400, "ServiceIdMissing", "service_id is required in the request body.")
    path = _resolve_service(catalog, service_id)
    if path is None:
        raise _Fault(
            400,
            "ServiceIdMissing",
            f"no service with id {service_id!r} in this broker's catalog. Fetch "
            f"/v2/catalog again; the golden paths on offer may have changed.",
        )
    if not path.enabled:
        raise _Fault(
            400,
            "ServiceIdMissing",
            f"the {path.id!r} service is disabled at this broker and cannot be provisioned.",
        )
    return path


def _require_plan(path: GoldenPath, body: Mapping[str, Any]) -> None:
    plan_id = body.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise _Fault(400, "PlanIdMissing", "plan_id is required in the request body.")
    # The literal "standard" is accepted alongside the derived uuid for the same reason
    # _resolve_service accepts a bare golden path id: it is what a human types with curl,
    # and every real platform sends back exactly what the catalog gave it.
    if plan_id.strip() not in (plan_id_for(path.id), "standard"):
        raise _Fault(
            400,
            "PlanIdMissing",
            f"plan {plan_id!r} does not belong to service {path.id!r}, which offers one "
            f"plan: {plan_id_for(path.id)}.",
        )


def _parameters(body: Mapping[str, Any]) -> dict[str, Any]:
    parameters = body.get("parameters", {})
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise _Fault(400, "InvalidParameters", "parameters must be a JSON object.")
    return dict(parameters)


def _existing_instance(
    settings: Settings, view: LeaseView, path: GoldenPath, parameters: Mapping[str, Any]
) -> Response:
    """Answer a PUT for an instance id that already has a lease.

    The spec's three cases -- identical and finished is 200, identical and still running
    is 202, different is 409 -- plus a fourth that OSB does not have a word for. A lease
    can *end*, and an instance whose lease has ended is neither "still exists" nor
    "available to create again": instance ids are not reusable, here or in the spec. So a
    PUT for a used-up id is a conflict, with a description that says why, rather than a
    200 that tells the platform it has an instance it no longer has.
    """
    if view.golden_path_id != path.id or _parameters_conflict(view, parameters):
        raise _Fault(
            409,
            "Conflict",
            f"service instance {view.id} already exists with different parameters. "
            f"Instance ids are permanent at this broker: use a new one.",
        )
    if view.state in _PROVISION_PENDING:
        return JSONResponse(
            status_code=202,
            content={
                "operation": "provision",
                "dashboard_url": _dashboard_url(settings, view.id),
            },
        )
    if view.state in (LeaseState.REJECTED, LeaseState.FAILED):
        raise _Fault(400, "PolicyDenied", _denial_text(view))
    if view.state not in _PROVISION_SUCCEEDED:
        raise _Fault(
            409,
            "Conflict",
            f"the lease behind this service instance is {view.state.value}: it has ended "
            f"or is ending, and the resource is gone or going. Instance ids are not "
            f"reusable; provision a new instance with a new id.",
        )
    return JSONResponse(
        status_code=200,
        content={
            "dashboard_url": _dashboard_url(settings, view.id),
            "metadata": _instance_metadata(view, ()),
        },
    )


def _denial_text(view: LeaseView) -> str:
    reason = view.policy_reason or view.failure_reason or "no reason was recorded"
    return (
        f"{reason} (lease {view.id}, golden path {view.golden_path_id}). This is a policy "
        f"decision, not a transient failure; retrying the same request will produce the "
        f"same answer."
    )


def _instance_metadata(view: LeaseView, notices: tuple[str, ...]) -> dict[str, Any]:
    """The ``metadata`` block, where ``expires_at`` is a standard OSB field since 2.15.

    Which is a genuinely useful coincidence: a platform that already understands
    ``metadata.expires_at`` will renew or warn about a bailment lease with no bailment
    -specific code at all.
    """
    metadata: dict[str, Any] = {}
    if view.expires_at is not None:
        metadata["expires_at"] = view.expires_at.isoformat()
    if view.seconds_remaining is not None:
        metadata["seconds_remaining"] = view.seconds_remaining
    metadata["renewals"] = {"used": view.renewals, "allowed": view.max_renewals}
    if view.outputs:
        # Only the outputs the golden path declares non-secret. The service filtered them
        # already; nothing here decrypts anything.
        metadata["outputs"] = dict(view.outputs)
    if view.secret_output_names:
        metadata["sealed_output_names"] = list(view.secret_output_names)
    if notices:
        metadata["notices"] = list(notices)
    return metadata


def _provision_progress(view: LeaseView) -> tuple[str, str]:
    """Map a lease state onto ``last_operation`` for a provision.

    ``activated_at`` rather than the current state decides success. A lease that went
    active and has since expired *did* provision successfully; reporting it as a failed
    provision would send somebody looking for a bug in the create path.
    """
    state = view.state
    if state is LeaseState.AWAITING_APPROVAL:
        reason = view.approval.reason if view.approval else "a human has to approve it"
        deadline = (
            f" The request expires at {view.approval.deadline_at.isoformat()} if nobody answers."
            if view.approval and view.approval.deadline_at
            else ""
        )
        return "in progress", f"Waiting for a human to approve this request: {reason}{deadline}"
    if state in _PROVISION_PENDING:
        return "in progress", f"Lease {view.id} is {state.value}; the resource does not exist yet."
    if state in _PROVISION_SUCCEEDED:
        remaining = view.seconds_remaining
        clock = f" It expires in {remaining}s." if remaining is not None else ""
        return "succeeded", f"Lease {view.id} is active.{clock} Bind to get its credentials."
    if state is LeaseState.REJECTED or (state is LeaseState.FAILED and view.activated_at is None):
        return "failed", _denial_text(view)
    if view.activated_at is not None:
        return (
            "succeeded",
            f"Lease {view.id} provisioned successfully and is now {state.value}.",
        )
    return "failed", view.failure_reason or f"Lease {view.id} ended as {state.value}."


def _deprovision_progress(view: LeaseView) -> tuple[str, str]:
    """Map a lease state onto ``last_operation`` for a deprovision.

    ``UNKNOWN`` and ``ORPHANED`` never report success. The provider has not confirmed the
    resource is gone, and telling a platform otherwise is precisely how a resource stops
    being anybody's problem while it carries on billing.
    """
    state = view.state
    if state is LeaseState.RELEASED:
        return "gone", "The resource is gone."
    if state in (
        LeaseState.EXPIRED,
        LeaseState.REVOKED,
        LeaseState.DEPROVISIONING,
        LeaseState.ACTIVE,
        LeaseState.EXPIRING,
    ):
        return "in progress", f"Lease {view.id} is {state.value}; a worker is destroying it."
    if state is LeaseState.ORPHANED:
        return (
            "failed",
            f"The resource for lease {view.id} could not be destroyed and may still exist: "
            f"{view.failure_reason or 'no detail recorded'}. It is visible as an orphan in "
            f"bailment and will be reported by every reconcile run until it is gone.",
        )
    if state is LeaseState.UNKNOWN:
        return (
            "in progress",
            f"The provider's state for lease {view.id} could not be determined. bailment "
            f"will keep asking rather than assume it was destroyed.",
        )
    # REJECTED or FAILED: nothing was ever created, so there is nothing left to delete.
    return "gone", f"Lease {view.id} is {state.value}; no resource was ever created."
