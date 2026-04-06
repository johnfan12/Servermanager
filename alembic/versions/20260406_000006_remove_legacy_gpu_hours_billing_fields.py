"""remove legacy gpu-hours billing fields

Revision ID: 20260406_000006
Revises: 20260405_000005
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260406_000006"
down_revision = "20260405_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_gpu_hour_ledgers_instance_id", table_name="gpu_hour_ledgers")
    op.drop_index("ix_gpu_hour_ledgers_user_id", table_name="gpu_hour_ledgers")
    op.drop_index("ix_gpu_hour_ledgers_id", table_name="gpu_hour_ledgers")
    op.drop_table("gpu_hour_ledgers")

    op.drop_column("instances", "last_billing_at")
    op.drop_column("users", "gpu_hours_frozen")
    op.drop_column("users", "gpu_hours_used")
    op.drop_column("users", "gpu_hours_quota")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("gpu_hours_quota", sa.Float(), nullable=False, server_default="100"),
    )
    op.add_column(
        "users",
        sa.Column("gpu_hours_used", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("gpu_hours_frozen", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("instances", sa.Column("last_billing_at", sa.DateTime(), nullable=True))

    op.create_table(
        "gpu_hour_ledgers",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=True),
        sa.Column(
            "delta_gpu_hours",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("reason", sa.String(length=64), nullable=False, server_default="settlement"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_gpu_hour_ledgers_id", "gpu_hour_ledgers", ["id"], unique=False)
    op.create_index(
        "ix_gpu_hour_ledgers_user_id", "gpu_hour_ledgers", ["user_id"], unique=False
    )
    op.create_index(
        "ix_gpu_hour_ledgers_instance_id",
        "gpu_hour_ledgers",
        ["instance_id"],
        unique=False,
    )

    op.alter_column("users", "gpu_hours_quota", server_default=None)
    op.alter_column("users", "gpu_hours_used", server_default=None)
    op.alter_column("users", "gpu_hours_frozen", server_default=None)
