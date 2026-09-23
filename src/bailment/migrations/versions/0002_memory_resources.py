"""The memory provider's shared store.

The sandbox's fake cloud used to live in each process's memory, so a reconciler running
anywhere but the worker's process saw an empty provider and released healthy leases. This
table is where it lives now, so every bailment process sees one fake cloud. The minted
credential is kept Fernet-sealed in ``sealed_outputs``; nothing credential-shaped is stored
in plain text.

Revision ID: 0002_memory_resources
Revises: 0001_baseline
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_memory_resources"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_resources",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_name", sa.String(length=255), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("simulate", sa.String(length=32), nullable=False),
        sa.Column("sealed_outputs", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider", "external_name"),
    )


def downgrade() -> None:
    op.drop_table("memory_resources")
