"""create servermanager tables"""

from alembic import op
import sqlalchemy as sa


revision = "20260321_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("quota_gpu", sa.Integer(), nullable=False, server_default="4"),
        sa.Column(
            "quota_memory_gb", sa.Integer(), nullable=False, server_default="64"
        ),
        sa.Column(
            "quota_max_instances", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "instances",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("container_name", sa.String(length=128), nullable=False),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        sa.Column("gpu_indices", sa.JSON(), nullable=False),
        sa.Column("memory_gb", sa.Integer(), nullable=False),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("ssh_port", sa.Integer(), nullable=True),
        sa.Column("ssh_password", sa.String(length=64), nullable=True),
        sa.Column("vps_access", sa.JSON(), nullable=True),
        sa.Column("image_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("expire_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("container_name"),
        sa.UniqueConstraint("container_id"),
        sa.UniqueConstraint("ssh_port"),
    )
    op.create_index(
        "ix_instances_container_name", "instances", ["container_name"], unique=True
    )
    op.create_index("ix_instances_user_id", "instances", ["user_id"], unique=False)
    op.create_index("ix_instances_status", "instances", ["status"], unique=False)
    op.create_index("ix_instances_expire_at", "instances", ["expire_at"], unique=False)

    op.create_table(
        "gpu_allocations",
        sa.Column("gpu_index", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=False),
        sa.Column("allocated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_gpu_allocations_instance_id",
        "gpu_allocations",
        ["instance_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_gpu_allocations_instance_id", table_name="gpu_allocations")
    op.drop_table("gpu_allocations")
    op.drop_index("ix_instances_expire_at", table_name="instances")
    op.drop_index("ix_instances_status", table_name="instances")
    op.drop_index("ix_instances_user_id", table_name="instances")
    op.drop_index("ix_instances_container_name", table_name="instances")
    op.drop_table("instances")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
