"""add instance snapshot fields

Revision ID: 20260405_000005
Revises: 20260404_000004
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa


revision = "20260405_000005"
down_revision = "20260404_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("base_image_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "instances",
        sa.Column("runtime_image_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "instances",
        sa.Column("last_snapshot_image_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "instances",
        sa.Column("last_snapshot_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "instances",
        sa.Column(
            "snapshot_status",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
    )
    op.execute("UPDATE instances SET base_image_name = image_name WHERE base_image_name IS NULL")
    op.execute(
        "UPDATE instances SET runtime_image_name = image_name WHERE runtime_image_name IS NULL"
    )
    op.alter_column("instances", "snapshot_status", server_default=None)


def downgrade() -> None:
    op.drop_column("instances", "snapshot_status")
    op.drop_column("instances", "last_snapshot_at")
    op.drop_column("instances", "last_snapshot_image_name")
    op.drop_column("instances", "runtime_image_name")
    op.drop_column("instances", "base_image_name")
