"""GPU status querying and allocation helpers."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from filelock import FileLock
from sqlalchemy.orm import Session, sessionmaker

from config import GPU_COUNT, LOCK_DIR
from models import GPUAllocation, Instance, User

LOGGER = logging.getLogger(__name__)

GPUStat = dict[str, int | float | str | None]
GPUStatus = dict[str, int | float | str | bool | None]


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

    @staticmethod
    def _parse_nvidia_int(value: str) -> int | None:
        """Parse one integer field from nvidia-smi, tolerating unsupported values."""
        normalized = value.strip()
        if not normalized or normalized.upper() == "N/A":
            return None
        try:
            return int(float(normalized))
        except ValueError:
            return None

    @staticmethod
    def _parse_nvidia_float(value: str) -> float | None:
        """Parse one float field from nvidia-smi, tolerating unsupported values."""
        normalized = value.strip()
        if not normalized or normalized.upper() == "N/A":
            return None
        try:
            return float(normalized)
        except ValueError:
            return None

    def _query_nvidia_smi(self) -> list[GPUStat]:
        """Query live GPU status from `nvidia-smi`."""
        command = [
            "nvidia-smi",
            (
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,"
                "temperature.gpu,power.draw,power.limit"
            ),
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
            if len(parts) != 8:
                continue
            gpu_index = self._parse_nvidia_int(parts[0])
            memory_total_mb = self._parse_nvidia_int(parts[2])
            memory_used_mb = self._parse_nvidia_int(parts[3])
            utilization_gpu = self._parse_nvidia_int(parts[4])
            temperature_c = self._parse_nvidia_int(parts[5])
            power_draw_w = self._parse_nvidia_float(parts[6])
            power_limit_w = self._parse_nvidia_float(parts[7])
            if gpu_index is None:
                continue
            gpus.append(
                {
                    "index": gpu_index,
                    "name": parts[1],
                    "memory_total_mb": memory_total_mb,
                    "memory_used_mb": memory_used_mb,
                    "utilization_gpu": utilization_gpu,
                    "temperature_c": temperature_c,
                    "power_draw_w": power_draw_w,
                    "power_limit_w": power_limit_w,
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
            # cluster_manager 适配：查询 gpu_allocations 表，join Instance 和 User
            # 构建 {gpu_index: username} 的映射，只包含 running 状态的实例
            allocations = (
                session.query(GPUAllocation)
                .join(Instance)
                .join(User)
                .filter(Instance.status == "running")
                .all()
            )
            allocation_map: dict[int, str] = {}
            live_map: dict[int, GPUStat] = {}
            for allocation in allocations:
                allocation_obj = cast(Any, allocation)
                gpu_index = int(allocation_obj.gpu_index)
                username = (
                    allocation_obj.instance.user.username
                    if allocation_obj.instance is not None
                    and allocation_obj.instance.user is not None
                    else None
                )
                if username:
                    allocation_map[gpu_index] = str(username)

            # 从 nvidia-smi 获取 GPU 型号和内存信息
            for gpu in live_status:
                gpu_index = int(gpu["index"])
                live_map[gpu_index] = gpu

            # cluster_manager 适配：遍历 range(GPU_COUNT) 生成完整列表
            enriched: list[GPUStatus] = []
            for gpu_index in range(GPU_COUNT):
                allocated_to = allocation_map.get(gpu_index)
                is_used = allocated_to is not None
                live_gpu = live_map.get(gpu_index, {})
                memory_total_mb = live_gpu.get("memory_total_mb")
                memory_used_mb = live_gpu.get("memory_used_mb")
                state: GPUStatus = {
                    "index": gpu_index,
                    "status": "used" if is_used else "free",
                    "is_idle": not is_used,
                    "allocated_to": allocated_to,
                    "name": live_gpu.get("name"),
                    "gpu_model": live_gpu.get("name"),
                    "memory_total_mb": memory_total_mb,
                    "memory_used_mb": memory_used_mb,
                    "memory_total_gb": (
                        int(memory_total_mb) // 1024
                        if isinstance(memory_total_mb, int)
                        else None
                    ),
                    "utilization_gpu": live_gpu.get("utilization_gpu"),
                    "temperature_c": live_gpu.get("temperature_c"),
                    "power_draw_w": live_gpu.get("power_draw_w"),
                    "power_limit_w": live_gpu.get("power_limit_w"),
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
