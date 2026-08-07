"""agent teams

Revision ID: 20260806_01
Revises: 20260805_01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_01"
down_revision: str | None = "20260805_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_teams",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("template_key", sa.String(), nullable=False),
        sa.Column("template_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_request_id"),
    )
    op.create_index(op.f("ix_agent_teams_user_id"), "agent_teams", ["user_id"], unique=False)
    op.create_index(op.f("ix_agent_teams_status"), "agent_teams", ["status"], unique=False)
    op.create_table(
        "agent_team_members",
        sa.Column("team_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("role_key", sa.String(), nullable=False),
        sa.Column("member_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["agent_teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("team_id", "agent_id"),
        sa.UniqueConstraint("team_id", "role_key"),
    )
    op.create_index(
        op.f("ix_agent_team_members_status"), "agent_team_members", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_team_members_status"), table_name="agent_team_members")
    op.drop_table("agent_team_members")
    op.drop_index(op.f("ix_agent_teams_status"), table_name="agent_teams")
    op.drop_index(op.f("ix_agent_teams_user_id"), table_name="agent_teams")
    op.drop_table("agent_teams")
