"""enforce exclusive team membership

Revision ID: 20260806_02
Revises: 20260806_01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260806_02"
down_revision: str | None = "20260806_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_agent_team_members_agent_id", "agent_team_members", ["agent_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_agent_team_members_agent_id", table_name="agent_team_members")
