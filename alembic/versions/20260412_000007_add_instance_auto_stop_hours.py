"""add instance auto stop hours"""

from alembic import op
import sqlalchemy as sa


revision = "20260412_000007"
down_revision = "20260406_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("auto_stop_hours", sa.Integer(), nullable=False, server_default="6"),
    )
    op.execute("UPDATE instances SET expire_at = NULL WHERE status != 'running'")


def downgrade() -> None:
    op.drop_column("instances", "auto_stop_hours")
