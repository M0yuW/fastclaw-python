"""session context snapshots

Revision ID: 20260824_01
Revises: 20260806_02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_01"
down_revision: str | None = "20260806_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_context_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("session_key", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("profile", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("compacted_through_message_id", sa.String(), nullable=False),
        sa.Column("source_digest", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("native_state", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "agent_id", "session_key"],
            ["sessions.user_id", "sessions.agent_id", "sessions.key"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "agent_id", "session_key", "generation",
            name="uq_session_context_snapshot_generation",
        ),
    )
    for column in ("user_id", "agent_id", "session_key", "created_at"):
        op.create_index(
            op.f(f"ix_session_context_snapshots_{column}"),
            "session_context_snapshots",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in ("created_at", "session_key", "agent_id", "user_id"):
        op.drop_index(
            op.f(f"ix_session_context_snapshots_{column}"),
            table_name="session_context_snapshots",
        )
    op.drop_table("session_context_snapshots")
