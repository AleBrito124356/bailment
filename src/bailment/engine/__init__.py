"""The engine: everything that turns a request into a resource and back again.

Four components, and the split between them is about what happens when each one dies.

:mod:`bailment.engine.service`
    Synchronous, request-scoped, never touches a provider. It decides, it names, it
    commits. If it crashes, nothing outside the database has happened yet.

:mod:`bailment.engine.worker`
    The only component that calls a provider's mutating methods. Written entirely around
    being killed mid-call: claims are conditional updates that expire on their own, and
    ``external_name`` is already committed before anything is created, so a replacement
    worker can ask the provider what actually happened.

:mod:`bailment.engine.leases`
    The clock. Warns, expires, renews, revokes, and times out approvals nobody answered.
    It only ever writes intentions -- ``EXPIRED``, ``REVOKED`` -- and leaves the part that
    can fail to a worker.

:mod:`bailment.engine.reconciler`
    Both directions of drift, and the reason this project exists. Resources at the
    provider with no live lease, and leases whose resource has vanished. Report-only by
    default; destroying an orphan needs two switches thrown deliberately.

They share one rule: **only a provider's confirmation can produce ``RELEASED``**. Every
other component asks for ``EXPIRED`` or ``REVOKED`` and lets the worker find out whether
reality agreed. That is what keeps a failed teardown distinguishable from a successful
one, and a failure you can distinguish is a failure you can fix.
"""

from __future__ import annotations

from bailment.engine.leases import (
    LeaseNotice,
    LeaseTicker,
    Notifier,
    RenewalError,
    TickReport,
    renew_lease,
    revoke_lease,
)
from bailment.engine.reconciler import (
    DEFAULT_GRACE,
    DriftRecord,
    OrphanRecord,
    ProviderReconcileResult,
    Reconciler,
    ReconcileSummary,
)
from bailment.engine.service import (
    ApprovalView,
    BindingNotFound,
    Caller,
    CallerKind,
    ConflictingState,
    InvalidRequest,
    LeaseNotFound,
    LeaseService,
    LeaseView,
    NotAuthorized,
    PathDisabled,
    ProvisionOutcome,
    ProvisionRequest,
    RenewalRefused,
    SecretAccessDenied,
    ServiceError,
    UnknownPath,
    clamp_ttl,
    make_external_name,
)
from bailment.engine.validation import InputValidationError, SchemaError, validate_inputs
from bailment.engine.worker import CLAIM_TTL, ProvisioningWorker, WorkerTick

__all__ = [
    "CLAIM_TTL",
    "DEFAULT_GRACE",
    "ApprovalView",
    "BindingNotFound",
    "Caller",
    "CallerKind",
    "ConflictingState",
    "DriftRecord",
    "InputValidationError",
    "InvalidRequest",
    "LeaseNotFound",
    "LeaseNotice",
    "LeaseService",
    "LeaseTicker",
    "LeaseView",
    "NotAuthorized",
    "Notifier",
    "OrphanRecord",
    "PathDisabled",
    "ProviderReconcileResult",
    "ProvisionOutcome",
    "ProvisionRequest",
    "ProvisioningWorker",
    "ReconcileSummary",
    "Reconciler",
    "RenewalError",
    "RenewalRefused",
    "SchemaError",
    "SecretAccessDenied",
    "ServiceError",
    "TickReport",
    "UnknownPath",
    "WorkerTick",
    "clamp_ttl",
    "make_external_name",
    "renew_lease",
    "revoke_lease",
    "validate_inputs",
]
