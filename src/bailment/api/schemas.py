"""Everything the REST API is allowed to say, and the guard that keeps it that way.

Every model in this module is a *response* shape or a *request* shape for the dashboard
API. They exist as a separate layer rather than as ``LeaseView.as_dict()`` calls in the
handlers for one reason: a response model is a thing somebody adds a field to at four in
the afternoon, and this is the file where that person has to be stopped.

**The rule.** No response model may carry a decrypted secret value. Not a connection
string, not a token, not "just for the operator", not behind a query parameter. A binding
response carries the reference, the output names and the access metadata -- never the
values. The one function in bailment that decrypts anything is
:meth:`bailment.engine.service.LeaseService.resolve_binding`, it is reachable from the CLI
injection path and nowhere else, and :mod:`bailment.api.routes` proves at import time that
it never calls it.

**Why the rule is absolute rather than "operators may see values".** An operator token
*is* permitted to decrypt -- :attr:`bailment.engine.service.Caller.may_read_secrets` is
true for :attr:`~bailment.engine.service.CallerKind.OPERATOR`. The refusal here is not
about authorisation. A value in an HTTP response body is a value in the reverse proxy's
access log, in the browser's memory, in the dashboard's DOM, in whatever APM tool records
response payloads, and in the tab somebody leaves open. Handing a credential to an
authorised human over HTTP still ends with the credential in six places nobody chose.
``bailment exec <lease-id> -- <command>`` puts it in exactly one process image instead.

**The guard.** :func:`assert_no_secret_fields` walks every model listed in
:data:`RESPONSE_MODELS`, and everything those models nest, and refuses any field whose
*name* looks like a credential -- reusing :func:`bailment.logging.is_secret_key`, which is
already this codebase's authority on that question. It runs at import time, so a response
model with a ``token`` field makes the API fail to start rather than fail an audit. It is
also exported, so the test suite asserts on it directly.

A name that trips the check and is genuinely safe goes in :data:`JUSTIFIED_FIELD_NAMES`
with a comment saying why. There is exactly one entry today and adding a second should
feel like a decision.

The guard is a name check, so it is a tripwire and not a proof. The proof is structural
and lives in :mod:`bailment.api.routes`: no handler can reach plaintext because no handler
references the only function that produces it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal, get_args

from fastapi import HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from bailment.catalog.schema import GoldenPath, LeasePolicy, PolicyEffect
from bailment.engine.reconciler import (
    DriftRecord,
    OrphanRecord,
    ProviderReconcileResult,
    ReconcileSummary,
)
from bailment.engine.service import ApprovalView, LeaseView, ProvisionOutcome, aware
from bailment.logging import is_secret_key, redact_secrets
from bailment.models import AuditEvent, ReconcileRun
from bailment.providers.registry import ProviderStatus
from bailment.states import LeaseState

__all__ = [
    "HOURS_PER_MONTH",
    "JUSTIFIED_FIELD_NAMES",
    "RESPONSE_MODELS",
    "USAGE_HINT",
    "ApprovalQueueResponse",
    "ApprovalResponse",
    "ApproveRequestBody",
    "AuditEventResponse",
    "AuditTrailResponse",
    "BindingListResponse",
    "BindingResponse",
    "CatalogDetailResponse",
    "CatalogEntryResponse",
    "CatalogListResponse",
    "CostResponse",
    "DriftResponse",
    "ErrorDetail",
    "ErrorEnvelope",
    "LeasePage",
    "LeaseResponse",
    "LeaseTermsResponse",
    "OrphanResponse",
    "OutputResponse",
    "PendingApprovalResponse",
    "PolicyRuleResponse",
    "ProviderListResponse",
    "ProviderReconcileResponse",
    "ProviderStatusResponse",
    "ProvisionRequestBody",
    "ProvisionResponse",
    "ReconcileRunDetailResponse",
    "ReconcileRunPage",
    "ReconcileRunResponse",
    "ReconcileTriggerResponse",
    "RejectRequestBody",
    "RenewRequestBody",
    "RevokeRequestBody",
    "StatsResponse",
    "assert_no_secret_fields",
    "http_error",
    "scrub_detail",
    "secret_looking_fields",
]

#: How a consumer actually uses a binding. Rendered into every binding response, because
#: the reference is useless on its own and an agent that is told what to do with it stops
#: asking for the value.
USAGE_HINT: Final = "bailment exec {lease_id} -- <command>"

#: 730 hours is the conventional cloud-billing month, matching
#: :class:`bailment.catalog.schema.CostModel`. Two different months in one product would
#: make the dashboard and the catalog disagree about the same lease.
HOURS_PER_MONTH: Final = 730.0


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def scrub_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Run a stored JSON blob through the log redactor before it leaves the process.

    Audit event details and reconcile run details are written by the workers and the
    reconciler, and both are careful: provider messages are already scrubbed by
    :func:`bailment.providers.base.scrub`, and only outputs a golden path declares
    non-secret are ever recorded in plaintext. This is the second layer, applied on the
    way out, because those blobs are free-form ``JSON`` columns and the next person to add
    a field to one will not read this file first.

    Reusing the log redactor rather than writing a second one is deliberate: one
    definition of "this looks like a credential", improved in one place.
    """
    return dict(redact_secrets(None, "api", dict(detail or {})))


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """The body of every error this API returns.

    ``error`` is a stable machine-readable code and ``message`` is written for whoever is
    blocked -- which, for this broker, is usually an agent that will otherwise simply try
    the same call again.
    """

    error: str = Field(description="Stable code, e.g. 'lease_not_found'.")
    message: str = Field(description="Written for the caller who was refused.")
    problems: list[str] = Field(
        default_factory=list,
        description="Per-field validation problems, when the request failed a schema.",
    )


class ErrorEnvelope(BaseModel):
    """What an error actually looks like on the wire.

    FastAPI wraps :class:`fastapi.HTTPException` detail in ``{"detail": ...}``, so the
    documented schema says so rather than pretending the body is bare.
    """

    detail: ErrorDetail


def http_error(
    status_code: int, code: str, message: str, problems: Sequence[str] = ()
) -> HTTPException:
    """Build the one error shape this API returns.

    Every refusal in :mod:`bailment.api.deps` and :mod:`bailment.api.routes` comes through
    here, and the body is constructed from :class:`ErrorDetail` rather than from a literal
    dict, so the documented schema and the actual response cannot drift apart.
    """
    return HTTPException(
        status_code=status_code,
        detail=ErrorDetail(error=code, message=message, problems=list(problems)).model_dump(),
    )


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------


class LeaseTermsResponse(BaseModel):
    """A golden path's lease policy, in both the human and the arithmetic form."""

    default_ttl: str
    default_ttl_seconds: int
    max_ttl: str
    max_ttl_seconds: int
    warn_before: str
    warn_before_seconds: int
    renewable: bool
    max_renewals: int

    @classmethod
    def from_policy(cls, policy: LeasePolicy) -> LeaseTermsResponse:
        return cls(
            default_ttl=str(policy.default_ttl),
            default_ttl_seconds=int(policy.default_ttl.delta.total_seconds()),
            max_ttl=str(policy.max_ttl),
            max_ttl_seconds=int(policy.max_ttl.delta.total_seconds()),
            warn_before=str(policy.warn_before),
            warn_before_seconds=int(policy.warn_before.delta.total_seconds()),
            renewable=policy.renewable,
            max_renewals=policy.max_renewals,
        )


class CostResponse(BaseModel):
    estimated_hourly_usd: float
    estimated_monthly_usd: float
    note: str = ""


class OutputResponse(BaseModel):
    """One value a lease on this path produces.

    ``secret`` is the classification, not a value: a secret output is sealed and the
    consumer receives a reference, a non-secret one is published in
    :attr:`LeaseResponse.outputs`.
    """

    name: str
    description: str = ""
    secret: bool


class PolicyRuleResponse(BaseModel):
    """One rule in a golden path's chain, with its position, since first match wins."""

    index: int
    when: str | None
    effect: PolicyEffect
    reason: str
    approvers: list[str] = Field(default_factory=list)


class CatalogEntryResponse(BaseModel):
    """A golden path as the catalog page renders it.

    ``provider_available`` is here so a caller can tell "we do not offer that" apart from
    "we offer it but nobody has configured the credential yet". An agent that cannot tell
    the difference retries the second one forever.
    """

    id: str
    name: str
    description: str
    provider: str
    provider_available: bool
    enabled: bool
    tags: list[str] = Field(default_factory=list)
    mcp_tool_name: str
    input_schema: dict[str, Any] = Field(
        description="JSON Schema for the request, including the universal 'ttl' argument."
    )
    lease: LeaseTermsResponse
    cost: CostResponse
    outputs: list[OutputResponse] = Field(default_factory=list)

    @classmethod
    def from_path(cls, path: GoldenPath, *, provider_available: bool) -> CatalogEntryResponse:
        return cls(
            id=path.id,
            name=path.name,
            description=path.description,
            provider=path.provider,
            provider_available=provider_available,
            enabled=path.enabled,
            tags=list(path.tags),
            mcp_tool_name=path.mcp_tool_name,
            # The schema an agent is given, not the raw one, so the form the dashboard
            # renders and the tool the agent calls cannot disagree about whether 'ttl'
            # is an argument.
            input_schema=path.input_schema_with_lease(),
            lease=LeaseTermsResponse.from_policy(path.lease),
            cost=CostResponse(
                estimated_hourly_usd=path.cost.estimated_hourly_usd,
                estimated_monthly_usd=path.cost.estimated_monthly_usd,
                note=path.cost.note,
            ),
            outputs=[
                OutputResponse(name=o.name, description=o.description, secret=o.secret)
                for o in path.outputs
            ],
        )


class CatalogDetailResponse(CatalogEntryResponse):
    """One golden path, including the policy chain that decides its requests."""

    policy: list[PolicyRuleResponse] = Field(default_factory=list)
    source: str | None = Field(
        default=None,
        description=(
            "File name this path was loaded from. Operators only, and the name alone -- "
            "the full path would publish the layout of the machine."
        ),
    )

    @classmethod
    def from_path_detail(
        cls, path: GoldenPath, *, provider_available: bool, source: str | None
    ) -> CatalogDetailResponse:
        base = CatalogEntryResponse.from_path(path, provider_available=provider_available)
        return cls(
            **base.model_dump(),
            policy=[
                PolicyRuleResponse(
                    index=index,
                    when=rule.when,
                    effect=rule.effect,
                    reason=rule.reason,
                    approvers=list(rule.approvers),
                )
                for index, rule in enumerate(path.policy)
            ],
            source=source,
        )


class CatalogListResponse(BaseModel):
    items: list[CatalogEntryResponse]
    count: int
    directory: str | None = Field(
        default=None, description="Where the catalog was loaded from. Operators only."
    )


# --------------------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------------------


class ApprovalResponse(BaseModel):
    id: str
    reason: str
    allowed_approvers: list[str] = Field(default_factory=list)
    requested_at: datetime
    deadline_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    approved: bool | None = None
    decision_note: str | None = None
    url: str | None = None
    pending: bool

    @classmethod
    def from_view(cls, view: ApprovalView) -> ApprovalResponse:
        return cls(
            id=view.id,
            reason=view.reason,
            allowed_approvers=list(view.allowed_approvers),
            requested_at=view.requested_at,
            deadline_at=view.deadline_at,
            decided_at=view.decided_at,
            decided_by=view.decided_by,
            approved=view.approved,
            decision_note=view.decision_note,
            url=view.url,
            pending=view.decided_at is None,
        )


class LeaseResponse(BaseModel):
    """A lease, in the one shape every caller gets.

    Mirrors :class:`bailment.engine.service.LeaseView` field for field, on purpose. There
    is one view of a lease in this system and not an agent view plus an operator view,
    because two renderings of the same row is how a field ends up in the wrong one.

    Two fields deserve a second look before anybody adds a third:

    ``outputs``
        Plaintext, and *only* the values the golden path declares ``secret: false`` --
        ``FQDN`` on the DNS path, ``SANDBOX_ID`` on the sandbox. The service filters them
        on write and again on read, so a path that reclassifies an output as secret takes
        effect for leases that were activated before the change. Nothing else may be put
        in here.

    ``secret_output_names``
        Names, never values. It exists so a consumer knows the shape of what it holds
        without anything being decrypted to tell it.
    """

    id: str
    golden_path: str
    provider: str
    state: LeaseState
    requester: str
    on_behalf_of: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    released_at: datetime | None = None
    seconds_remaining: int | None = None

    ttl_seconds: int
    max_ttl_seconds: int
    renewals: int
    max_renewals: int
    renewable: bool

    policy_effect: str | None = None
    policy_reason: str | None = None

    external_name: str | None = None
    estimated_hourly_usd: float

    binding_reference: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    secret_output_names: list[str] = Field(default_factory=list)

    approval: ApprovalResponse | None = None
    failure_reason: str | None = None
    usable: bool

    @classmethod
    def from_view(cls, view: LeaseView) -> LeaseResponse:
        return cls(
            id=view.id,
            golden_path=view.golden_path_id,
            provider=view.provider,
            state=view.state,
            requester=view.requester,
            on_behalf_of=view.on_behalf_of,
            inputs=dict(view.inputs),
            created_at=view.created_at,
            activated_at=view.activated_at,
            expires_at=view.expires_at,
            released_at=view.released_at,
            seconds_remaining=view.seconds_remaining,
            ttl_seconds=view.ttl_seconds,
            max_ttl_seconds=view.max_ttl_seconds,
            renewals=view.renewals,
            max_renewals=view.max_renewals,
            renewable=view.renewable,
            policy_effect=view.policy_effect,
            policy_reason=view.policy_reason,
            external_name=view.external_name,
            estimated_hourly_usd=view.estimated_hourly_usd,
            binding_reference=view.binding_reference,
            outputs=dict(view.outputs),
            secret_output_names=list(view.secret_output_names),
            approval=ApprovalResponse.from_view(view.approval) if view.approval else None,
            failure_reason=view.failure_reason,
            usable=view.usable,
        )


class ProvisionResponse(BaseModel):
    """The answer to a provision or a renewal.

    ``notices`` is the channel that teaches a caller its bounds -- a clamped TTL puts a
    sentence in here naming the ceiling. Agents read it and stop guessing.
    """

    lease: LeaseResponse
    replayed: bool = False
    ttl_clamped: bool = False
    notices: list[str] = Field(default_factory=list)

    @classmethod
    def from_outcome(cls, outcome: ProvisionOutcome) -> ProvisionResponse:
        return cls(
            lease=LeaseResponse.from_view(outcome.lease),
            replayed=outcome.replayed,
            ttl_clamped=outcome.ttl_clamped,
            notices=list(outcome.notices),
        )


class LeasePage(BaseModel):
    """One page of leases.

    There is no ``total``. Counting would mean writing the visibility predicate a second
    time in a second place, and a filter that has to agree with
    :meth:`bailment.engine.service.LeaseService.list_leases` in two files is a filter that
    will eventually disagree -- in the direction where somebody sees a lease that is not
    theirs. ``has_more`` is derived by asking for one row more than the page size, which
    needs no second query and no second predicate. Aggregate counts live on ``/stats``,
    which applies the predicate once.
    """

    items: list[LeaseResponse]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None = None


# --------------------------------------------------------------------------------------
# Bindings -- read this before adding a field
# --------------------------------------------------------------------------------------


class BindingResponse(BaseModel):
    """A binding's *metadata*. Never, under any circumstances, its values.

    ================== ==================================================================
    Belongs here       reference, output names, timestamps, access count, revocation
    Never here         DATABASE_URL, REDIS_TOKEN, any password, any connection string,
                       any element of the sealed payload, encoded or otherwise
    ================== ==================================================================

    If you are here because somebody asked to "just show the value in the dashboard": the
    answer is ``bailment exec``. It decrypts in one process, injects into that process's
    child, and never writes the value anywhere that outlives it. Adding a field here
    instead puts the credential in the proxy log, the browser, and every APM tool that
    records response bodies -- and no rotation can un-see any of them.

    ``access_count`` is the field that makes this endpoint worth having. A binding whose
    count climbs while its lease sits idle is the anomaly you want to notice.
    """

    lease_id: str
    reference: str
    output_names: list[str] = Field(
        description="Which values the sealed envelope contains. Names only."
    )
    created_at: datetime
    last_accessed_at: datetime | None = None
    access_count: int
    revoked_at: datetime | None = None
    usable: bool = Field(
        description="Whether this binding can still be injected: not revoked, lease live."
    )
    how_to_use: str


class BindingListResponse(BaseModel):
    """Every binding a lease has produced, newest first, including revoked ones.

    Revoked bindings are kept in the answer rather than filtered out because "this
    credential was resolved four times before the resource was destroyed" is exactly the
    question an incident asks, and a list that only shows live bindings cannot answer it.
    """

    lease_id: str
    items: list[BindingResponse]


# --------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------


class AuditEventResponse(BaseModel):
    """One row of the append-only trail.

    ``detail`` passes through :func:`scrub_detail` on the way out. See that function for
    why a second layer exists over blobs that are already written carefully.
    """

    id: str
    at: datetime
    actor: str
    action: str
    from_state: str | None = None
    to_state: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_event(cls, event: AuditEvent) -> AuditEventResponse:
        return cls(
            id=event.id,
            at=aware(event.at),
            actor=event.actor,
            action=event.action,
            from_state=event.from_state,
            to_state=event.to_state,
            detail=scrub_detail(event.detail),
        )


class AuditTrailResponse(BaseModel):
    """A lease's history, oldest first, because it is a story and stories start at 1."""

    lease_id: str
    items: list[AuditEventResponse]
    limit: int
    offset: int
    has_more: bool


# --------------------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------------------


class PendingApprovalResponse(BaseModel):
    """One row of an operator's approval inbox.

    Purpose-built rather than a full :class:`LeaseResponse`, and it carries exactly what a
    person needs in order to decide: who asked, on whose behalf, for what, at what cost,
    for how long, and how much time is left before the request times out on its own.

    ``seconds_until_deadline`` goes negative for a request whose window has closed but
    which the ticker has not swept yet. Rendering that as ``0`` would tell an approver
    they still have time to act on something that can no longer be approved.
    """

    lease_id: str
    golden_path: str
    provider: str
    state: LeaseState
    requester: str
    on_behalf_of: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int
    estimated_hourly_usd: float
    reason: str
    allowed_approvers: list[str] = Field(default_factory=list)
    policy_reason: str | None = None
    requested_at: datetime
    deadline_at: datetime | None = None
    seconds_until_deadline: int | None = None
    waiting_seconds: int


class ApprovalQueueResponse(BaseModel):
    items: list[PendingApprovalResponse]
    limit: int
    offset: int
    has_more: bool


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


class OrphanResponse(BaseModel):
    """A resource that exists at a provider with no live lease behind it."""

    provider: str
    external_name: str
    provider_resource_id: str
    reason: str
    created_at: datetime | None = None
    age_seconds: int | None = None
    lease_id: str | None = None
    lease_state: str | None = None
    destroyed: bool = False
    destroy_error: str | None = None

    @classmethod
    def from_record(cls, record: OrphanRecord) -> OrphanResponse:
        return cls(**record.as_dict())


class DriftResponse(BaseModel):
    """A lease we believed was live whose resource has vanished underneath us."""

    provider: str
    lease_id: str
    external_name: str
    from_state: str
    to_state: str
    note: str

    @classmethod
    def from_record(cls, record: DriftRecord) -> DriftResponse:
        return cls(**record.as_dict())


class ProviderReconcileResponse(BaseModel):
    """One provider's pass.

    ``checked`` is not decoration. A provider that could not be reached reports zero
    orphans, and a dashboard that cannot tell that apart from a clean account is a
    dashboard that is reassuring and wrong.
    """

    provider: str
    checked: bool
    skipped_reason: str | None = None
    destroy_armed: bool
    resources_seen: int
    leases_checked: int
    within_grace: int
    in_flight_skipped: int
    raced: int
    known_orphan_leases: int
    status_unknown: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    orphans: list[OrphanResponse] = Field(default_factory=list)
    drift: list[DriftResponse] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: ProviderReconcileResult) -> ProviderReconcileResponse:
        return cls(
            provider=result.provider,
            checked=result.checked,
            skipped_reason=result.skipped_reason,
            destroy_armed=result.destroy_armed,
            resources_seen=result.resources_seen,
            leases_checked=result.leases_checked,
            within_grace=result.within_grace,
            in_flight_skipped=result.in_flight_skipped,
            raced=result.raced,
            known_orphan_leases=result.known_orphan_leases,
            status_unknown=list(result.status_unknown),
            errors=list(result.errors),
            orphans=[OrphanResponse.from_record(o) for o in result.orphans],
            drift=[DriftResponse.from_record(d) for d in result.drift],
        )


class ReconcileTriggerResponse(BaseModel):
    """The result of a reconcile run started from the API.

    ``destroy_armed`` is always false and the field is here so that is visible rather than
    assumed. Destroying an orphan takes two switches thrown deliberately, and an HTTP
    request is one. See :meth:`bailment.engine.reconciler.Reconciler.may_destroy`.
    """

    started_at: datetime
    finished_at: datetime | None = None
    destroy_armed: bool = False
    clean: bool
    orphans_found: int
    orphans_destroyed: int
    drift_found: int
    resources_seen: int
    leases_checked: int
    status_unknown: int
    errors: int
    providers_checked: list[str] = Field(default_factory=list)
    providers_skipped: list[str] = Field(default_factory=list)
    providers: list[ProviderReconcileResponse] = Field(default_factory=list)

    @classmethod
    def from_summary(cls, summary: ReconcileSummary) -> ReconcileTriggerResponse:
        return cls(
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            destroy_armed=False,
            clean=summary.clean,
            orphans_found=summary.orphans_found,
            orphans_destroyed=summary.orphans_destroyed,
            drift_found=summary.drift_found,
            resources_seen=summary.resources_seen,
            leases_checked=summary.leases_checked,
            status_unknown=summary.status_unknown,
            errors=summary.errors,
            providers_checked=list(summary.providers_checked),
            providers_skipped=list(summary.providers_skipped),
            providers=[ProviderReconcileResponse.from_result(p) for p in summary.providers],
        )


class ReconcileRunResponse(BaseModel):
    """A stored run, as the drift-over-time chart reads it.

    ``clean`` is computed the same way :attr:`ReconcileSummary.clean` computes it, which
    means a run with an unknown status is not clean. "We could not tell" must never render
    as "there was nothing there".
    """

    id: str
    provider: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    resources_seen: int
    leases_checked: int
    orphans_found: int
    orphans_destroyed: int
    drift_found: int
    errors: int
    status_unknown: int
    destroy_armed: bool
    clean: bool

    @classmethod
    def from_row(cls, run: ReconcileRun) -> ReconcileRunResponse:
        detail = dict(run.detail or {})
        unknown = detail.get("status_unknown")
        status_unknown = len(unknown) if isinstance(unknown, list) else 0
        started = aware(run.started_at)
        finished = aware(run.finished_at) if run.finished_at is not None else None
        return cls(
            id=run.id,
            provider=run.provider,
            started_at=started,
            finished_at=finished,
            duration_seconds=(
                round((finished - started).total_seconds(), 3) if finished is not None else None
            ),
            resources_seen=run.resources_seen,
            leases_checked=run.leases_checked,
            orphans_found=run.orphans_found,
            orphans_destroyed=run.orphans_destroyed,
            drift_found=run.drift_found,
            errors=run.errors,
            status_unknown=status_unknown,
            destroy_armed=bool(detail.get("destroy_armed", False)),
            clean=(
                run.orphans_found == 0
                and run.drift_found == 0
                and run.errors == 0
                and status_unknown == 0
            ),
        )


class ReconcileRunDetailResponse(ReconcileRunResponse):
    """A stored run plus the findings blob, which is where the orphan list lives."""

    detail: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row_detail(cls, run: ReconcileRun) -> ReconcileRunDetailResponse:
        base = ReconcileRunResponse.from_row(run)
        return cls(**base.model_dump(), detail=scrub_detail(run.detail))


class ReconcileRunPage(BaseModel):
    items: list[ReconcileRunResponse]
    limit: int
    offset: int
    has_more: bool


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------


class ProviderStatusResponse(BaseModel):
    """Whether a provider can be called, and if not, which setting is missing.

    ``missing_settings`` holds environment variable *names* -- ``BAILMENT_NEON_API_KEY``,
    not its value. Naming an unset setting is how an operator fixes it; a broker that
    answered "misconfigured" and stopped there would cost somebody an afternoon.
    """

    name: str
    available: bool
    reason: str | None = None
    supports_reconciliation: bool
    missing_settings: list[str] = Field(default_factory=list)
    golden_paths: list[str] = Field(default_factory=list)

    @classmethod
    def from_status(
        cls,
        status: ProviderStatus,
        *,
        missing_settings: Sequence[str],
        golden_paths: Sequence[str],
    ) -> ProviderStatusResponse:
        return cls(
            name=status.name,
            available=status.available,
            reason=status.reason,
            supports_reconciliation=status.supports_reconciliation,
            missing_settings=list(missing_settings),
            golden_paths=list(golden_paths),
        )


class ProviderListResponse(BaseModel):
    items: list[ProviderStatusResponse]


# --------------------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------------------


class StatsResponse(BaseModel):
    """The dashboard header.

    ``scope`` is part of the payload because the same endpoint answers two different
    questions: an operator sees the whole installation, anybody else sees only their own
    leases. A number whose scope is implicit is a number somebody will screenshot into an
    incident review with the wrong caption.

    ``estimated_hourly_usd`` sums every lease in a state where a real resource is believed
    to exist -- including ``ORPHANED`` and ``UNKNOWN``. Excluding those would make the
    spend figure agree with what bailment *intended* rather than with what is running,
    and the gap between those two is the entire point of this project.
    """

    scope: Literal["all", "own"]
    at: datetime
    active_leases: int = Field(description="Leases usable right now: active or expiring.")
    expiring_soon: int
    expiring_within_seconds: int
    awaiting_approval: int
    orphans_outstanding: int
    live_leases: int = Field(description="Leases believed to have a real resource behind them.")
    total_leases: int
    estimated_hourly_usd: float
    estimated_daily_usd: float
    estimated_monthly_usd: float
    by_state: dict[str, int] = Field(default_factory=dict)
    last_reconcile: ReconcileRunResponse | None = Field(
        default=None, description="Most recent stored reconcile run. Operators only."
    )


# --------------------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------------------


class ProvisionRequestBody(BaseModel):
    """A provisioning request.

    ``extra="forbid"`` because a request with a misspelled key is a request that silently
    did something other than what was meant, and the caller here is frequently a model
    that will not notice.
    """

    model_config = ConfigDict(extra="forbid")

    golden_path: str = Field(
        validation_alias=AliasChoices("golden_path", "golden_path_id"),
        description="Golden path id, e.g. 'postgres'.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Arguments, validated against the path's schema."
    )
    ttl: str | None = Field(
        default=None,
        description=(
            "Compact duration such as '2h' or '45m'. Omit for the path's default. A value "
            "above the path's ceiling is clamped, not rejected, and the response says so."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Two calls with the same key return the same lease, never two resources. May "
            "also be sent as the Idempotency-Key header."
        ),
    )


class RenewRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl: str | None = Field(
        default=None,
        description=(
            "How much longer, measured from now rather than from the old deadline. Omit "
            "for the path's default."
        ),
    )


class RevokeRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Goes in the audit trail. Write it for whoever reads it in a month.",
    )


class ApproveRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class RejectRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Shown verbatim to the requester, who is usually an agent that will otherwise "
            "simply try again."
        ),
    )


# --------------------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------------------

#: Field names that trip :func:`bailment.logging.is_secret_key` and are nonetheless safe.
#: Every entry needs a reason. If you are adding one, the question to answer is not "is
#: this field sensitive" but "could this field ever hold a value that opens something".
JUSTIFIED_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        # Names of the sealed values, so a consumer knows the shape of what it holds.
        # A name is not a credential and this list is already public in the catalog.
        "secret_output_names",
        # A boolean classification on a catalog entry -- "this output will be sealed" --
        # taken straight from the golden path YAML, which is a file people read in code
        # review. It describes how a value is treated; it is never a value.
        "secret",
    }
)

#: Every model this API can return. :func:`assert_no_secret_fields` walks these and
#: everything they nest, so a new endpoint's response model belongs in here.
RESPONSE_MODELS: Final[tuple[type[BaseModel], ...]] = (
    ApprovalQueueResponse,
    ApprovalResponse,
    AuditEventResponse,
    AuditTrailResponse,
    BindingListResponse,
    BindingResponse,
    CatalogDetailResponse,
    CatalogEntryResponse,
    CatalogListResponse,
    ErrorEnvelope,
    LeasePage,
    LeaseResponse,
    ProviderListResponse,
    ProvisionResponse,
    ReconcileRunDetailResponse,
    ReconcileRunPage,
    ReconcileTriggerResponse,
    StatsResponse,
)


def _nested_models(annotation: object) -> Iterator[type[BaseModel]]:
    """Every pydantic model reachable from a type annotation.

    Walks ``list[X]``, ``X | None``, ``dict[str, X]`` and so on through
    :func:`typing.get_args`, because a secret smuggled into a response would arrive nested
    inside a container far more plausibly than as a top-level field.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from _nested_models(argument)


def secret_looking_fields(model: type[BaseModel]) -> list[str]:
    """``Model.field`` for every field whose name looks like it could hold a credential.

    Recurses into nested models. Returns an empty list for a model that is safe.
    """
    found: list[str] = []
    seen: set[type[BaseModel]] = set()

    def walk(current: type[BaseModel]) -> None:
        if current in seen:
            return
        seen.add(current)
        for name, field in current.model_fields.items():
            if name not in JUSTIFIED_FIELD_NAMES and is_secret_key(name):
                found.append(f"{current.__name__}.{name}")
            for nested in _nested_models(field.annotation):
                walk(nested)

    walk(model)
    return found


def assert_no_secret_fields(
    models: Iterable[type[BaseModel]] = RESPONSE_MODELS,
) -> None:
    """Refuse to serve if any response model could carry a credential by name.

    Called at import time, so this is a startup failure rather than an audit finding, and
    exported so the test suite can assert on it by name. A field that legitimately trips
    it goes in :data:`JUSTIFIED_FIELD_NAMES` with a comment -- and if the honest comment
    is hard to write, the field is the problem.
    """
    offenders: list[str] = []
    for model in models:
        offenders.extend(secret_looking_fields(model))
    if offenders:
        raise RuntimeError(
            "a bailment API response model declares a field whose name suggests it "
            "carries a credential: "
            + ", ".join(sorted(set(offenders)))
            + ". No API response may contain a decrypted value -- a binding response "
            "carries the reference, the output names and the metadata, and consumers use "
            "'bailment exec <lease-id> -- <command>' to get the value into a process "
            "without it passing through an HTTP body. If the field is genuinely safe, add "
            "its name to bailment.api.schemas.JUSTIFIED_FIELD_NAMES with the reason."
        )


assert_no_secret_fields()
