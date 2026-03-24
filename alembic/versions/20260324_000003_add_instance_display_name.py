"""add instance display_name

Revision ID: 20260324_000003
Revises: 20260323_000002
Create Date: 2026-03-24 00:03:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260324_000003"
down_revision = "20260323_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=128), nullable=True))

    op.execute("UPDATE instances SET display_name = container_name WHERE display_name IS NULL")

    with op.batch_alter_table("instances") as batch_op:
        batch_op.alter_column(
            "display_name", existing_type=sa.String(length=128), nullable=False
        )
        batch_op.create_unique_constraint(
            "uq_instances_user_display_name", ["user_id", "display_name"]
        )


def downgrade() -> None:
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_constraint("uq_instances_user_display_name", type_="unique")
        batch_op.drop_column("display_name")
