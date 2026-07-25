"""Persistence model.

Two decisions in here carry most of the weight, and both exist to make orphaned
resources findable rather than to make the happy path prettier.

**Write-ahead naming.** ``Lease.external_name`` is computed and committed *before* the
provider is called, never after. If the worker dies between the create call and the
response, the row already knows the name and tag the resource was going to carry, so
the reconciler can go and look for it. Systems that name resources from the provider's
response cannot do this: a crash in that window leaves a real resource that nothing on
earth can associate back to a request.

**Claims, not locks.** A worker takes a row by writing ``claimed_by``/``claimed_at``
under a conditional update. A claim expires on its own, so a worker that dies holding
one does not wedge the lease forever -- the claim goes stale and another worker picks
it up. There is no distributed lock to leak.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bailment.states import LeaseState


def utcnow() -> datetime:
    """Timezone-aware now. Never use ``datetime.utcnow()``; it returns a naive value."""
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base with a portable JSON type.

    ``JSON`` rather than ``JSONB`` so the same models run on SQLite for tests and CI
    without anyone needing a Postgres to contribute a one-line fix.
    """

    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class Lease(Base):
    """A provisioning request and, if it succeeds, the lease that results from it.

    Request and lease are one row on purpose. Splitting them means a failed request has
    no lease row, which means the most interesting failures -- the ones where a resource
    was half-created -- have nowhere to live.
    """

    __tablename__ = "leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    # --- What was asked for ------------------------------------------------
    golden_path_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # --- Who asked ---------------------------------------------------------
    requester: Mapped[str] = mapped_column(String(255), index=True)
    """The principal that called the API. For agent traffic this is the agent identity."""

    on_behalf_of: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    """The human the agent is acting for. Null for direct human requests.

    Kept distinct from ``requester`` because "which agent did this" and "who is
    accountable for it" are different questions and an audit log that conflates them is
    useless in exactly the incident where you need it.
    """

    agent_session: Mapped[str | None] = mapped_column(String(255), default=None)
    """Opaque session id from the MCP client, so one runaway session can be traced."""

    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)
    """Caller-supplied. Two calls with the same key return the same lease, never two."""

    # --- Lifecycle ---------------------------------------------------------
    state: Mapped[str] = mapped_column(String(32), default=LeaseState.PENDING, index=True)
    previous_state: Mapped[str | None] = mapped_column(String(32), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    ttl_seconds: Mapped[int] = mapped_column(Integer, default=0)
    renewals: Mapped[int] = mapped_column(Integer, default=0)

    # --- Policy ------------------------------------------------------------
    policy_effect: Mapped[str | None] = mapped_column(String(32), default=None)
    policy_reason: Mapped[str | None] = mapped_column(Text, default=None)
    policy_rule_index: Mapped[int | None] = mapped_column(Integer, default=None)

    # --- Provider linkage. The orphan-detection payload. -------------------
    external_name: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    """Deterministic name/tag written BEFORE the provider is called. See module docstring."""

    provider_resource_id: Mapped[str | None] = mapped_column(String(512), default=None)
    """The provider's own id, once known. May be null while a real resource exists."""

    provider_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Everything needed to destroy the resource without consulting anything else."""

    estimated_hourly_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # --- Worker claim ------------------------------------------------------
    claimed_by: Mapped[str | None] = mapped_column(String(128), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)

    bindings: Mapped[list[Binding]] = relationship(
        back_populates="lease", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[AuditEvent]] = relationship(
        back_populates="lease", cascade="all, delete-orphan", lazy="selectin"
    )
    approval: Mapped[Approval | None] = relationship(
        back_populates="lease", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    __table_args__ = (
        # Partial uniqueness would be nicer, but this is portable across SQLite and
        # Postgres and the application already skips null keys before inserting.
        UniqueConstraint("requester", "idempotency_key", name="uq_lease_idempotency"),
        Index("ix_lease_state_next_attempt", "state", "next_attempt_at"),
        Index("ix_lease_provider_external", "provider", "external_name"),
    )

    @property
    def lease_state(self) -> LeaseState:
        return LeaseState(self.state)

    @property
    def seconds_remaining(self) -> int | None:
        if self.expires_at is None:
            return None
        return int((self.expires_at - utcnow()).total_seconds())

    @property
    def is_claim_stale(self) -> bool:
        """Whether another worker may steal this claim."""
        if self.claimed_at is None:
            return True
        return utcnow() - self.claimed_at > timedelta(minutes=5)


class Binding(Base):
    """The credentials a lease produced.

    Secret values are stored encrypted and are never returned to an agent. An agent
    receives :attr:`reference` and can ask bailment to inject the value into a process
    it launches, which keeps the credential out of the model's context window entirely.
    """

    __tablename__ = "bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lease_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("leases.id", ondelete="CASCADE"), index=True
    )

    reference: Mapped[str] = mapped_column(String(255), unique=True)
    """Opaque handle, e.g. ``bailment://binding/3f2a...``. Safe to log and to show a model."""

    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    """Fernet-encrypted JSON of the output values. Never written in plaintext."""

    output_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    """Which keys the payload contains, so the UI can show shape without decrypting."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    access_count: Mapped[int] = mapped_column(Integer, default=0)

    lease: Mapped[Lease] = relationship(back_populates="bindings")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class Approval(Base):
    """A human decision gate on one lease."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lease_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("leases.id", ondelete="CASCADE"), unique=True
    )

    reason: Mapped[str] = mapped_column(Text)
    allowed_approvers: Mapped[list[str]] = mapped_column(JSON, default=list)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    """An approval request that nobody answers must expire, or the queue becomes a graveyard."""

    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decided_by: Mapped[str | None] = mapped_column(String(255), default=None)
    approved: Mapped[bool | None] = mapped_column(Boolean, default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)

    lease: Mapped[Lease] = relationship(back_populates="approval")

    @property
    def is_pending(self) -> bool:
        return self.decided_at is None


class AuditEvent(Base):
    """Append-only record of everything that happened to a lease.

    Never updated, never deleted while the lease exists. If you find yourself wanting to
    mutate a row here, what you actually want is another row.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lease_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("leases.id", ondelete="CASCADE"), index=True
    )

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), default=None)
    to_state: Mapped[str | None] = mapped_column(String(32), default=None)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    lease: Mapped[Lease] = relationship(back_populates="events")


class ReconcileRun(Base):
    """One pass of the reconciler, kept so drift can be tracked over time.

    The counters here are the project's headline metric: a platform where
    ``orphans_found`` is reliably zero is a platform whose teardown path actually works.
    """

    __tablename__ = "reconcile_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(64), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    resources_seen: Mapped[int] = mapped_column(Integer, default=0)
    leases_checked: Mapped[int] = mapped_column(Integer, default=0)

    orphans_found: Mapped[int] = mapped_column(Integer, default=0)
    """Resources that exist at the provider with no live lease accounting for them."""

    orphans_destroyed: Mapped[int] = mapped_column(Integer, default=0)
    drift_found: Mapped[int] = mapped_column(Integer, default=0)
    """Leases we believed were active whose resource has vanished underneath us."""

    errors: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
