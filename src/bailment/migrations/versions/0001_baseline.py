"""Baseline: the schema bailment 0.1.0 created with ``create_all``.

Every table as it stood before migrations existed -- leases, bindings, approvals,
audit_events, reconcile_runs -- with the same column types, constraint shapes and index
names ``Base.metadata.create_all`` produced. That sameness is the point: a database a 0.1.0
install created by hand is recognised by ``bailment db upgrade``, compared against this
revision, and stamped rather than recreated. Unnamed unique and foreign-key constraints
stay unnamed, so the names Postgres assigns are the ones ``create_all`` got too.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("golden_path_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("requester", sa.String(length=255), nullable=False),
        sa.Column("on_behalf_of", sa.String(length=255), nullable=True),
        sa.Column("agent_session", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("renewals", sa.Integer(), nullable=False),
        sa.Column("policy_effect", sa.String(length=32), nullable=True),
        sa.Column("policy_reason", sa.Text(), nullable=True),
        sa.Column("policy_rule_index", sa.Integer(), nullable=True),
        sa.Column("external_name", sa.String(length=255), nullable=True),
        sa.Column("provider_resource_id", sa.String(length=512), nullable=True),
        sa.Column("provider_ref", sa.JSON(), nullable=False),
        sa.Column("estimated_hourly_usd", sa.Float(), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("requester", "idempotency_key", name="uq_lease_idempotency"),
    )
    op.create_index(
        "ix_lease_provider_external", "leases", ["provider", "external_name"], unique=False
    )
    op.create_index(
        "ix_lease_state_next_attempt", "leases", ["state", "next_attempt_at"], unique=False
    )
    for column in (
        "expires_at",
        "external_name",
        "golden_path_id",
        "next_attempt_at",
        "on_behalf_of",
        "provider",
        "requester",
        "state",
    ):
        op.create_index(op.f(f"ix_leases_{column}"), "leases", [column], unique=False)

    op.create_table(
        "reconcile_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resources_seen", sa.Integer(), nullable=False),
        sa.Column("leases_checked", sa.Integer(), nullable=False),
        sa.Column("orphans_found", sa.Integer(), nullable=False),
        sa.Column("orphans_destroyed", sa.Integer(), nullable=False),
        sa.Column("drift_found", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_reconcile_runs_provider"), "reconcile_runs", ["provider"], unique=False
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lease_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("allowed_approvers", sa.JSON(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_id"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lease_id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("action", "at", "lease_id"):
        op.create_index(op.f(f"ix_audit_events_{column}"), "audit_events", [column], unique=False)

    op.create_table(
        "bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lease_id", sa.String(length=36), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("output_names", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference"),
    )
    op.create_index(op.f("ix_bindings_lease_id"), "bindings", ["lease_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bindings_lease_id"), table_name="bindings")
    op.drop_table("bindings")
    for column in ("lease_id", "at", "action"):
        op.drop_index(op.f(f"ix_audit_events_{column}"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("approvals")
    op.drop_index(op.f("ix_reconcile_runs_provider"), table_name="reconcile_runs")
    op.drop_table("reconcile_runs")
    for column in (
        "state",
        "requester",
        "provider",
        "on_behalf_of",
        "next_attempt_at",
        "golden_path_id",
        "external_name",
        "expires_at",
    ):
        op.drop_index(op.f(f"ix_leases_{column}"), table_name="leases")
    op.drop_index("ix_lease_state_next_attempt", table_name="leases")
    op.drop_index("ix_lease_provider_external", table_name="leases")
    op.drop_table("leases")
