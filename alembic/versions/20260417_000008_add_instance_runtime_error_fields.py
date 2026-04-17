"""add instance runtime error fields"""

from alembic import op
import sqlalchemy as sa


revision = "20260417_000008"
down_revision = "20260412_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("last_exit_code", sa.Integer(), nullable=True))
    op.add_column("instances", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("instances", "last_error")
    op.drop_column("instances", "last_exit_code")
