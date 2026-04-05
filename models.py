"""SQLAlchemy ORM models for users, instances, and GPU allocations."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    """Application user with resource quotas."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    quota_gpu = Column(Integer, nullable=False, default=4)
    quota_memory_gb = Column(Integer, nullable=False, default=64)
    quota_max_instances = Column(Integer, nullable=False, default=3)
    gpu_hours_quota = Column(Float, nullable=False, default=100.0)
    gpu_hours_used = Column(Float, nullable=False, default=0.0)
    gpu_hours_frozen = Column(Float, nullable=False, default=0.0)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    instances = relationship(
        "Instance", back_populates="user", cascade="all, delete-orphan"
    )
    gpu_hour_ledgers = relationship(
        "GPUHourLedger", back_populates="user", cascade="all, delete-orphan"
    )
    ssh_keys = relationship(
        "UserSSHKey", back_populates="user", cascade="all, delete-orphan"
    )


class Instance(Base):
    """Managed container instance owned by a user."""

    __tablename__ = "instances"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "display_name", name="uq_instances_user_display_name"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    container_name = Column(String(128), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=False)
    container_id = Column(String(128), unique=True, nullable=True)
    gpu_indices = Column(JSON, nullable=False, default=list)
    memory_gb = Column(Integer, nullable=False)
    cpu_cores = Column(Integer, nullable=False)
    ssh_port = Column(Integer, nullable=True, unique=True)
    ssh_password = Column(String(64), nullable=True)
    vps_access = Column(JSON, nullable=True)  # VPS 访问信息: {vps_port, vps_ip, ssh_cmd}
    image_name = Column(String(128), nullable=False)
    base_image_name = Column(String(255), nullable=True)
    runtime_image_name = Column(String(255), nullable=True)
    last_snapshot_image_name = Column(String(255), nullable=True)
    last_snapshot_at = Column(DateTime, nullable=True)
    snapshot_status = Column(String(32), nullable=False, default="none")
    status = Column(String(32), nullable=False, default="stopped")
    last_billing_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    stopped_at = Column(DateTime, nullable=True)
    expire_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="instances")
    gpu_allocations = relationship(
        "GPUAllocation",
        back_populates="instance",
        cascade="all, delete-orphan",
    )
    gpu_hour_ledgers = relationship(
        "GPUHourLedger", back_populates="instance", cascade="all, delete-orphan"
    )


class GPUAllocation(Base):
    """Current GPU allocation rows for running instances."""

    __tablename__ = "gpu_allocations"

    gpu_index = Column(Integer, primary_key=True)
    instance_id = Column(
        Integer, ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    allocated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    instance = relationship("Instance", back_populates="gpu_allocations")


class GPUHourLedger(Base):
    """Billing ledger rows for GPU-hour increments."""

    __tablename__ = "gpu_hour_ledgers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instance_id = Column(
        Integer,
        ForeignKey("instances.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    delta_gpu_hours = Column(Float, nullable=False, default=0.0)
    reason = Column(String(64), nullable=False, default="settlement")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="gpu_hour_ledgers")
    instance = relationship("Instance", back_populates="gpu_hour_ledgers")


class UserSSHKey(Base):
    """Node-local copy of one user's SSH public key."""

    __tablename__ = "user_ssh_keys"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "fingerprint",
            name="uq_user_ssh_keys_user_id_fingerprint",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    public_key = Column(Text, nullable=False)
    remark = Column(String(255), nullable=False, default="")
    fingerprint = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship("User", back_populates="ssh_keys")
