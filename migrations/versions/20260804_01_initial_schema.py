"""Create the initial FastClaw persistence schema."""

from alembic import op

from fastclaw.storage.models import Base

revision = "20260804_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
