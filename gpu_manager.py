"""GPU status querying and allocation helpers."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from filelock import FileLock
from sqlalchemy.orm import Session, sessionmaker

from config import LOCK_DIR
from models import GPUAllocation, Instance, User

LOGGER = logging.getLogger(__name__)

GPUStat = dict[str, int | str]
GPUStatus = dict[str, int | str | bool | None]


class GPUManager:
    """Coordinate GPU inspection and allocation with a file lock."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Initialize the manager with a SQLAlchemy session factory."""
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.session_factory = session_factory
        self.lock = FileLock(str(LOCK_DIR / "gpu_allocations.lock"))

    @contextmanager
    def locked_allocation(self) -> Iterator[None]:
        """Hold the allocation lock during resource-sensitive operations."""
        with self.lock:
            yield

    def _query_nvidia_smi(self) -> list[GPUStat]:
        """Query live GPU status from `nvidia-smi`."""
        command = [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=True, timeout=10
            )
        except FileNotFoundError:
            LOGGER.warning("nvidia-smi not found; GPU status is unavailable.")
            return []
        except subprocess.SubprocessError as exc:
            LOGGER.warning("Failed to query nvidia-smi: %s", exc)
            return []

        gpus: list[GPUStat] = []
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 6:
                continue
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_total_mb": int(parts[2]),
                    "memory_used_mb": int(parts[3]),
                    "utilization_gpu": int(parts[4]),
                    "temperature_c": int(parts[5]),
                }
            )
        return gpus

    def is_gpu_idle(self, _gpu: GPUStat) -> bool:
        """Return whether a GPU is free from system-managed allocation."""
        return True

    def get_gpu_status(self, db: Session | None = None) -> list[GPUStatus]:
        """Return current GPU state merged with allocation metadata."""
        owns_session = db is None
        session = db if db is not None else self.session_factory()

        try:
            live_status = self._query_nvidia_smi()
            allocations = session.query(GPUAllocation).join(Instance).all()
            allocation_map: dict[int, GPUAllocation] = {}
            for allocation in allocations:
                allocation_obj = cast(Any, allocation)
                allocation_map[int(allocation_obj.gpu_index)] = allocation

            enriched: list[GPUStatus] = []
            for gpu in live_status:
                gpu_index = int(gpu["index"])
                allocation = allocation_map.get(gpu_index)
                allocation_obj = (
                    cast(Any, allocation) if allocation is not None else None
                )
                owner = (
                    allocation_obj.instance.user.username
                    if allocation_obj is not None
                    and allocation_obj.instance is not None
                    and allocation_obj.instance.user is not None
                    else None
                )
                container_name = (
                    allocation_obj.instance.container_name
                    if allocation_obj is not None
                    and allocation_obj.instance is not None
                    else None
                )
                state = {
                    **gpu,
                    "owner": owner,
                    "container_name": container_name,
                    "is_idle": self.is_gpu_idle(gpu) and allocation is None,
                }
                enriched.append(state)

            return enriched
        finally:
            if owns_session:
                session.close()

    def allocate(
        self,
        user: User,
        gpu_indices: list[int],
        memory_gb: int,
        cpu_cores: int,
        db: Session,
    ) -> list[GPUStatus]:
        """Validate that requested GPUs can be allocated to the user."""
        user_obj = cast(Any, user)
        if not gpu_indices:
            raise ValueError("At least one GPU must be selected for allocation.")

        if len(gpu_indices) > int(user_obj.quota_gpu):
            raise ValueError("Requested GPU count exceeds your quota.")
        if memory_gb > int(user_obj.quota_memory_gb):
            raise ValueError("Requested memory exceeds your quota.")
        if cpu_cores < len(gpu_indices) * 8:
            raise ValueError("CPU allocation must be at least 8 cores per GPU.")

        statuses = self.get_gpu_status(db)
        if not statuses:
            raise ValueError("No GPUs were detected on this server.")

        status_map: dict[int, GPUStatus] = {}
        for status in statuses:
            gpu_index_value = status.get("index")
            if isinstance(gpu_index_value, int):
                status_map[gpu_index_value] = status
        current_allocations = {
            allocation.gpu_index for allocation in db.query(GPUAllocation).all()
        }
        missing = [gpu for gpu in gpu_indices if gpu not in status_map]
        if missing:
            raise ValueError(f"Requested GPU(s) not found: {missing}")

        for gpu_index in gpu_indices:
            if gpu_index in current_allocations:
                raise ValueError(f"GPU {gpu_index} is already allocated.")
            if not status_map[gpu_index]["is_idle"]:
                raise ValueError(f"GPU {gpu_index} is currently busy.")

        return [status_map[gpu_index] for gpu_index in gpu_indices]

    def release(self, container_name: str, db: Session) -> int:
        """Release GPU allocation rows for a container name."""
        instance = (
            db.query(Instance).filter(Instance.container_name == container_name).first()
        )
        if not instance:
            return 0

        released = (
            db.query(GPUAllocation)
            .filter(GPUAllocation.instance_id == instance.id)
            .delete(synchronize_session=False)
        )
        return released
