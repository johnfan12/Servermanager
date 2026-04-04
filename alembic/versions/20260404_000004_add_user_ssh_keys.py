"""add user ssh keys

Revision ID: 20260404_000004
Revises: 20260324_000003
Create Date: 2026-04-04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260404_000004"
down_revision = "20260324_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_ssh_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "fingerprint",
            name="uq_user_ssh_keys_user_id_fingerprint",
        ),
    )
    op.create_index(op.f("ix_user_ssh_keys_id"), "user_ssh_keys", ["id"], unique=False)
    op.create_index(
        op.f("ix_user_ssh_keys_user_id"),
        "user_ssh_keys",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_ssh_keys_user_id"), table_name="user_ssh_keys")
    op.drop_index(op.f("ix_user_ssh_keys_id"), table_name="user_ssh_keys")
    op.drop_table("user_ssh_keys")
