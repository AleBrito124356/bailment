"""The HTTP surface a human, a dashboard or a script talks to.

Three modules, split by what each one is allowed to know:

:mod:`bailment.api.deps`
    Who is calling. Resolves a bearer token to a
    :class:`~bailment.engine.service.Caller` in one of two tiers -- operator or agent --
    and hands out the request-scoped session, catalog, registry and service.

:mod:`bailment.api.schemas`
    What the API is allowed to *say*. Every response shape lives here, together with the
    import-time check that none of them can carry a decrypted credential.

:mod:`bailment.api.routes`
    The endpoints, and the import-time proof that no handler can reach plaintext at all.

The application object itself is not here. This package exports a
:data:`~bailment.api.routes.router` mounted at ``/api/v1``; assembling it into a FastAPI
app -- middleware, lifespan, the dashboard, ``/healthz`` -- belongs to whatever composes
the process, and keeping the two apart is what lets the MCP server and the tests use these
routes without inheriting a web application's startup. Composing it looks like::

    app.include_router(router)
    install_error_handlers(app)        # optional; one error envelope instead of two
    app.state.settings = settings      # optional; each falls back to a process default
    app.state.catalog = catalog
    app.state.registry = registry
    app.state.sessionmaker = sessionmaker

**One rule governs everything in here.** No response body may contain a decrypted secret
value, for any caller, under any code path. An agent receives a
``bailment://binding/<uuid>`` reference; an operator receives the same reference, because
the refusal is not about authorisation -- it is that a credential in an HTTP body is a
credential in the proxy log, the browser and every tool that records response payloads.
The value reaches a process through ``bailment exec <lease-id> -- <command>`` and nowhere
else. Two guards enforce it and both run at import time:
:func:`~bailment.api.schemas.assert_no_secret_fields` over the response models, and
:func:`~bailment.api.routes.assert_handlers_never_decrypt` over the compiled handlers.
"""

from __future__ import annotations

from bailment.api.deps import (
    ANONYMOUS_PRINCIPAL,
    ON_BEHALF_OF_HEADER,
    SESSION_HEADER,
    CallerDep,
    CatalogDep,
    OperatorDep,
    RegistryDep,
    ServiceDep,
    SessionDep,
    SessionmakerDep,
    SettingsDep,
    current_caller,
    current_catalog,
    current_registry,
    current_settings,
    db_session,
    lease_service,
    operator_caller,
)
from bailment.api.routes import (
    API_PREFIX,
    DECRYPTION_NAMES,
    assert_handlers_never_decrypt,
    install_error_handlers,
    router,
)
from bailment.api.schemas import (
    RESPONSE_MODELS,
    ErrorDetail,
    ErrorEnvelope,
    LeaseResponse,
    ProvisionRequestBody,
    ProvisionResponse,
    assert_no_secret_fields,
    http_error,
)

__all__ = [
    "ANONYMOUS_PRINCIPAL",
    "API_PREFIX",
    "DECRYPTION_NAMES",
    "ON_BEHALF_OF_HEADER",
    "RESPONSE_MODELS",
    "SESSION_HEADER",
    "CallerDep",
    "CatalogDep",
    "ErrorDetail",
    "ErrorEnvelope",
    "LeaseResponse",
    "OperatorDep",
    "ProvisionRequestBody",
    "ProvisionResponse",
    "RegistryDep",
    "ServiceDep",
    "SessionDep",
    "SessionmakerDep",
    "SettingsDep",
    "assert_handlers_never_decrypt",
    "assert_no_secret_fields",
    "current_caller",
    "current_catalog",
    "current_registry",
    "current_settings",
    "db_session",
    "http_error",
    "install_error_handlers",
    "lease_service",
    "operator_caller",
    "router",
]
