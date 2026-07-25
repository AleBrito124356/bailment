"""Who is calling, and what this request is allowed to reach.

Two tiers, and the split is the one the rest of the system already understands.

**operator** -- an ``BAILMENT_ADMIN_TOKENS`` entry. Maps to
:attr:`~bailment.engine.service.CallerKind.OPERATOR`: sees every lease, approves, rejects,
revokes anybody's lease, re-drives a stuck teardown, triggers a reconcile.

**agent** -- a ``BAILMENT_API_TOKENS`` entry. Maps to
:attr:`~bailment.engine.service.CallerKind.AGENT`: requests leases and reads its own. It
cannot approve -- an approval gate the gated thing can open is not a gate -- and it can
never resolve a binding, which the service enforces on caller *kind* rather than on a
scope string precisely so that no token configuration can produce a reading agent.

There is no third tier for "a human with an ordinary token". A plain token is agent-tier,
because the interesting question this broker asks is not "is this a person" but "may this
principal end up holding a credential", and the safe answer for anything that is not an
explicitly-configured operator is no.

----

**Anonymous is agent-tier.** ``BAILMENT_ALLOW_ANONYMOUS`` exists for the local demo and it
grants the *lower* tier, never the higher one. The alternative -- letting the demo switch
also hand out approval rights -- would mean one environment variable in one compose file
turns a broker into an open one. A demo that wants to watch an approval land sets
``BAILMENT_ADMIN_TOKENS`` as well; that is one extra line, and it is the line that should
be hard to set by accident.

**A presented token that matches nothing is a 401, not an anonymous session.** Falling back
would turn a typo'd token into a silently-downgraded identity, and the audit trail would
record ``anonymous`` for a request somebody believes they made as themselves.

**Everything comes off ``app.state`` when it is there.** The catalog, the provider registry
and the session factory are looked up on the application first and only then fall back to
the process-wide defaults. That is what lets a test build an app around an in-memory
database and a two-path catalog without monkeypatching module globals -- and it is why
:func:`db_session` exists here rather than reusing :func:`bailment.db.get_session`, which
is hard-wired to the process-wide factory.

**No :class:`~bailment.secrets.SecretBox` is ever built.** :func:`lease_service`
deliberately does not pass one, so a deployment with no ``BAILMENT_ENCRYPTION_KEY``
configured still serves the whole dashboard: the API has no code path that decrypts
anything, so it has no reason to need the key. The day someone adds an endpoint that
does, this will be the line that stops compiling, which is the intent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Final, cast

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.api.schemas import ErrorDetail, http_error
from bailment.catalog.loader import Catalog, CatalogError, aload_catalog
from bailment.config import Settings, get_settings
from bailment.db import get_sessionmaker
from bailment.engine.service import Caller, CallerKind, LeaseService
from bailment.logging import get_logger
from bailment.providers.registry import ProviderRegistry, default_registry

__all__ = [
    "ANONYMOUS_PRINCIPAL",
    "MAX_PRINCIPAL_LENGTH",
    "ON_BEHALF_OF_HEADER",
    "SESSION_HEADER",
    "CallerDep",
    "CatalogDep",
    "OperatorDep",
    "RegistryDep",
    "ServiceDep",
    "SessionDep",
    "SessionmakerDep",
    "SettingsDep",
    "current_caller",
    "current_catalog",
    "current_registry",
    "current_sessionmaker",
    "current_settings",
    "db_session",
    "lease_service",
    "operator_caller",
]

log = get_logger("bailment.api.deps")

#: The principal recorded for unauthenticated requests when they are allowed at all.
ANONYMOUS_PRINCIPAL: Final = "anonymous"

#: ``Lease.requester``, ``Lease.on_behalf_of`` and ``Lease.agent_session`` are all
#: ``String(255)``. Refusing an over-long value here produces a 400 that names the header;
#: letting it through produces a database error on commit that names nothing useful.
MAX_PRINCIPAL_LENGTH: Final = 255

#: The human an agent is acting for. An audit annotation and nothing else: it grants no
#: access, because every visibility check in the service already passes for a caller that
#: is the lease's own requester.
ON_BEHALF_OF_HEADER: Final = "X-Bailment-On-Behalf-Of"

#: Opaque MCP session id, so one runaway agent session can be traced end to end.
SESSION_HEADER: Final = "X-Bailment-Agent-Session"

#: ``auto_error=False`` so that a missing token produces this module's message about how
#: to authenticate rather than FastAPI's bare "Not authenticated".
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="bailment token",
    description=(
        "A token from BAILMENT_API_TOKENS (agent tier) or BAILMENT_ADMIN_TOKENS (operator tier)."
    ),
)

#: Guards the one-time catalog load in :func:`current_catalog`. Without it, a burst of
#: requests against a cold application would each read and parse the whole catalog
#: directory, and the last one to finish would win.
_catalog_lock = asyncio.Lock()


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=ErrorDetail(error="unauthenticated", message=message).model_dump(),
        # Without this header a browser and half the HTTP clients in existence treat the
        # 401 as a hard failure rather than as a prompt for credentials.
        headers={"WWW-Authenticate": "Bearer"},
    )


# --------------------------------------------------------------------------------------
# Application-scoped objects
# --------------------------------------------------------------------------------------


def current_settings(request: Request) -> Settings:
    """Settings for this request: the application's, or the process-wide ones."""
    candidate = getattr(request.app.state, "settings", None)
    return candidate if isinstance(candidate, Settings) else get_settings()


SettingsDep = Annotated[Settings, Depends(current_settings)]


async def current_catalog(request: Request, settings: SettingsDep) -> Catalog:
    """The golden path catalog.

    Set by the application at startup in any normal deployment. The fallback -- load it
    once, off the event loop, and cache it on ``app.state`` -- exists so that an
    application which forgot to do that still serves rather than returning 503 forever,
    and so that a test can mount the router with nothing but a database.

    A catalog that cannot be loaded is a 503 and not a 500: the request was fine, this
    installation is not, and the message says which directory could not be read.
    """
    existing = getattr(request.app.state, "catalog", None)
    if isinstance(existing, Catalog):
        return existing

    async with _catalog_lock:
        existing = getattr(request.app.state, "catalog", None)
        if isinstance(existing, Catalog):
            return existing
        try:
            catalog = await aload_catalog(settings.catalog_dir)
        except CatalogError as exc:
            log.error("catalog could not be loaded", error=str(exc))
            raise http_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "catalog_unavailable",
                f"the golden path catalog could not be loaded, so nothing can be "
                f"provisioned or listed: {exc}",
            ) from None
        request.app.state.catalog = catalog
        log.info(
            "catalog loaded on first request",
            directory=str(catalog.directory),
            paths=len(catalog),
        )
        return catalog


CatalogDep = Annotated[Catalog, Depends(current_catalog)]


def current_registry(request: Request) -> ProviderRegistry:
    """The provider registry, including providers that are registered but unconfigured.

    Unavailable providers are kept, not filtered: the difference between "we do not offer
    that" and "nobody has set the API key yet" is the difference between a caller giving
    up and a caller telling an operator which variable to set.
    """
    existing = getattr(request.app.state, "registry", None)
    return existing if isinstance(existing, ProviderRegistry) else default_registry()


RegistryDep = Annotated[ProviderRegistry, Depends(current_registry)]


def current_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    """The session factory. Needed whole -- not just a session -- by the reconciler."""
    existing = getattr(request.app.state, "sessionmaker", None)
    if isinstance(existing, async_sessionmaker):
        # ``isinstance`` can only narrow to the unparameterised class; the factory an
        # application puts on app.state is the same one bailment.db builds.
        return cast("async_sessionmaker[AsyncSession]", existing)
    return get_sessionmaker()


SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(current_sessionmaker)]


async def db_session(
    factory: SessionmakerDep,
) -> AsyncIterator[AsyncSession]:
    """One session per request. Rolls back on error and deliberately never commits.

    Same contract as :func:`bailment.db.get_session` and for the same reason: a handler
    that returned 200 having decided not to write must not have work committed behind its
    back on the way out. Every mutating path in this API goes through
    :class:`~bailment.engine.service.LeaseService`, which commits its own writes because
    the durability of a lease row is part of its meaning.
    """
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(db_session)]


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


def _header_value(raw: str | None, header: str) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_PRINCIPAL_LENGTH:
        raise http_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            f"{header} is {len(value)} characters; the maximum is {MAX_PRINCIPAL_LENGTH}",
        )
    return value


async def current_caller(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    on_behalf_of: Annotated[str | None, Header(alias=ON_BEHALF_OF_HEADER)] = None,
    agent_session: Annotated[str | None, Header(alias=SESSION_HEADER)] = None,
) -> Caller:
    """Resolve the bearer token to a :class:`~bailment.engine.service.Caller`.

    The token comparison itself lives in :meth:`bailment.config.Settings.lookup_token`,
    which uses :func:`hmac.compare_digest` and scans every entry rather than returning on
    the first match, so the work does not depend on where in the list a token sits.
    """
    token = credentials.credentials.strip() if credentials is not None else None
    identity = settings.lookup_token(token) if token else None

    if identity is None:
        if token:
            log.warning("rejected an unrecognised bearer token")
            raise _unauthorized(
                "that bearer token is not configured on this broker. Tokens come from "
                "BAILMENT_API_TOKENS or BAILMENT_ADMIN_TOKENS."
            )
        if not settings.allow_anonymous:
            raise _unauthorized(
                "this endpoint needs a bearer token. Set BAILMENT_API_TOKENS (agent tier) "
                "or BAILMENT_ADMIN_TOKENS (operator tier) and send "
                "'Authorization: Bearer <token>'."
            )
        principal = ANONYMOUS_PRINCIPAL
        kind = CallerKind.AGENT
    else:
        principal = identity.principal
        # Admin means operator; everything else is agent tier. See the module docstring
        # for why there is no middle tier.
        kind = CallerKind.OPERATOR if identity.admin else CallerKind.AGENT

    return Caller(
        principal=principal,
        kind=kind,
        on_behalf_of=_header_value(on_behalf_of, ON_BEHALF_OF_HEADER),
        session=_header_value(agent_session, SESSION_HEADER),
    )


CallerDep = Annotated[Caller, Depends(current_caller)]


def operator_caller(caller: CallerDep) -> Caller:
    """The same caller, or 403 if it is not operator tier.

    403 rather than 404: the caller authenticated successfully and this endpoint exists.
    Pretending otherwise would be an honest-looking lie that costs an operator ten minutes
    of wondering whether they typed the URL wrong. Individual *leases*, by contrast, do
    answer 404 for a caller who may not see them -- confirming that a lease id is real is
    an enumeration oracle, and that is the service's call to make, not this one's.
    """
    if not caller.is_operator:
        raise http_error(
            status.HTTP_403_FORBIDDEN,
            "operator_required",
            "this endpoint needs an operator token (BAILMENT_ADMIN_TOKENS). Agent tokens "
            "can request leases and read their own.",
        )
    return caller


OperatorDep = Annotated[Caller, Depends(operator_caller)]


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


def lease_service(
    session: SessionDep,
    catalog: CatalogDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> LeaseService:
    """A service bound to this request's session.

    No ``secret_box``. The service builds one lazily on first use, and the only method
    that uses it is ``resolve_binding``, which no handler in this package calls -- see
    :func:`bailment.api.routes.assert_handlers_never_decrypt`, which proves it at import
    time. An API process therefore never needs BAILMENT_ENCRYPTION_KEY, and a deployment
    missing one still serves every page of the dashboard.
    """
    return LeaseService(session, catalog=catalog, registry=registry, settings=settings)


ServiceDep = Annotated[LeaseService, Depends(lease_service)]
