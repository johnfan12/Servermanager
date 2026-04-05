"""Docker-backed container lifecycle management."""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import secrets
import shlex
import socket
import string
from datetime import datetime
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import DeviceRequest
from filelock import FileLock
from requests import exceptions as requests_exceptions

from config import (
    DATA_DIR,
    DEFAULT_PIDS_LIMIT,
    FALLBACK_DATA_DIR,
    LOCK_DIR,
    PORT_RANGE,
    SNAPSHOT_COMMIT_TIMEOUT_SECONDS,
)
from frp_manager import FrpManager

LOGGER = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SNAPSHOT_IMAGE_REPOSITORY = "servermanager-snapshots"


class ContainerManager:
    """Manage Docker containers used as per-user GPU instances."""

    def __init__(self) -> None:
        """Initialize the container manager."""
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self._client: docker.DockerClient | None = None
        self.port_lock = FileLock(str(LOCK_DIR / "ports.lock"))
        self.frp_manager = FrpManager()
        self._data_root = self._resolve_data_root()

    def _resolve_data_root(self) -> Path:
        """Choose a writable workspace root once at startup."""
        primary_root = Path(DATA_DIR)
        fallback_root = Path(FALLBACK_DATA_DIR)

        try:
            primary_root.mkdir(parents=True, exist_ok=True)
            probe = primary_root / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return primary_root
        except Exception:
            fallback_root.mkdir(parents=True, exist_ok=True)
            LOGGER.warning(
                "DATA_DIR %s is not writable; using fallback root %s",
                DATA_DIR,
                fallback_root,
            )
            return fallback_root

    def _docker_client(self) -> docker.DockerClient:
        """Return a lazily initialized Docker client."""
        if self._client is not None:
            try:
                self._client.ping()
            except DockerException as exc:
                LOGGER.warning(
                    "Docker client became stale, recreating connection: %s", exc
                )
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
        if self._client is None:
            try:
                self._client = docker.from_env()
                self._client.ping()
            except DockerException as exc:
                raise RuntimeError(
                    "Unable to connect to Docker. Make sure Docker Engine is installed and running, "
                    "or that Docker Desktop WSL integration is enabled for this distro. "
                    f"Original error: {exc}"
                ) from exc
        return self._client

    def _generate_password(self, length: int = 8) -> str:
        """Generate a simple random SSH password."""
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _safe_path(self, base_dir: Path, child_name: str) -> Path:
        """Return a child path constrained to remain under its base directory."""
        base_path = base_dir.resolve(strict=False)
        candidate = (base_path / child_name).resolve(strict=False)
        if os.path.commonpath([str(base_path), str(candidate)]) != str(base_path):
            raise RuntimeError(f"Unsafe path segment rejected: {child_name}")
        return candidate

    def _validated_username(self, username: str) -> str:
        """Validate a username before using it in host paths."""
        if not USERNAME_RE.fullmatch(username):
            raise RuntimeError("Username contains unsupported characters.")
        return username

    def _validated_segment(self, value: str, field_name: str) -> str:
        """Validate an internal path segment before using it in host paths."""
        if not PATH_SEGMENT_RE.fullmatch(value):
            raise RuntimeError(f"{field_name} contains unsupported characters.")
        return value

    def _ensure_user_data_dir(self, username: str) -> Path:
        """Ensure the per-user data root directory exists."""
        safe_username = self._validated_username(username)
        user_dir = self._safe_path(self._data_root, safe_username)
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            return user_dir
        except OSError as exc:
            raise RuntimeError(
                f"Failed to prepare user workspace directory under {self._data_root}: {exc}"
            ) from exc

    def get_instance_workspace_dir(
        self, username: str, container_name: str, create: bool = True
    ) -> Path:
        """Return the dedicated workspace directory for an instance."""
        safe_container_name = self._validated_segment(container_name, "Container name")
        workspace_dir = self._safe_path(
            self._ensure_user_data_dir(username), safe_container_name
        )
        if create:
            workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    def locate_instance_workspace_dir(self, username: str, container_name: str) -> Path:
        """Locate an existing workspace, supporting legacy shared user directories."""
        safe_username = self._validated_username(username)
        safe_container_name = self._validated_segment(container_name, "Container name")
        roots = [
            self._safe_path(Path(DATA_DIR), safe_username),
            self._safe_path(Path(FALLBACK_DATA_DIR), safe_username),
        ]
        for root in roots:
            dedicated_dir = self._safe_path(root, safe_container_name)
            if dedicated_dir.exists():
                return dedicated_dir
        for root in roots:
            if root.exists():
                return root
        raise RuntimeError(
            f"Workspace for instance {container_name} was not found for user {username}."
        )

    def locate_instance_workspace_cleanup_dir(
        self, username: str, container_name: str
    ) -> Path | None:
        """Return a dedicated instance workspace that can be safely removed."""
        safe_username = self._validated_username(username)
        safe_container_name = self._validated_segment(container_name, "Container name")
        roots = [
            self._safe_path(Path(DATA_DIR), safe_username),
            self._safe_path(Path(FALLBACK_DATA_DIR), safe_username),
        ]
        shared_root_found = False
        for root in roots:
            dedicated_dir = self._safe_path(root, safe_container_name)
            if dedicated_dir.exists():
                return dedicated_dir
            if root.exists():
                shared_root_found = True
        if shared_root_found:
            LOGGER.warning(
                "Instance %s for user %s appears to use a legacy shared workspace; "
                "skipping automatic workspace deletion.",
                container_name,
                username,
            )
        return None

    def _workspace_helper_image(self) -> str:
        """Return a locally available image that can perform root file operations."""
        preferred = [
            "lab/base:22.04",
            "lab/pytorch:2.3-cuda12.1",
            "lab/pytorch:2.1-cuda11.8",
            "lab/tensorflow:2.15",
        ]
        dynamic = [item["image_ref"] for item in self.list_local_images()]
        candidates = list(dict.fromkeys(preferred + dynamic))

        for image_ref in candidates:
            try:
                self._docker_client().images.get(image_ref)
                return image_ref
            except ImageNotFound:
                continue
            except DockerException as exc:
                raise RuntimeError(
                    f"Failed to inspect Docker image {image_ref}: {exc}"
                ) from exc
        raise RuntimeError(
            "No local helper image is available for workspace file operations. "
            "Build or pull at least one image with /bin/sh and coreutils first."
        )

    def _run_workspace_helper(self, host_mount_dir: Path, shell_command: str) -> None:
        """Run a root-owned helper container for workspace file operations."""
        try:
            self._docker_client().containers.run(
                self._workspace_helper_image(),
                command=["/bin/sh", "-lc", shell_command],
                remove=True,
                working_dir="/workspace",
                volumes={str(host_mount_dir): {"bind": "/workspace", "mode": "rw"}},
            )
        except DockerException as exc:
            raise RuntimeError(
                f"Workspace helper operation failed for {host_mount_dir}: {exc}"
            ) from exc

    def copy_workspace(self, source_dir: Path, target_dir: Path) -> None:
        """Copy a workspace directory with metadata preserved."""
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        common_dir = Path(os.path.commonpath([str(source_dir), str(target_dir.parent)]))
        source_rel = Path(source_dir).relative_to(common_dir)
        target_rel = Path(target_dir).relative_to(common_dir)
        shell_command = (
            f"mkdir -p {shlex.quote(str(target_rel))} && "
            f"cp -a {shlex.quote(str(source_rel))}/. {shlex.quote(str(target_rel))}"
        )
        self._run_workspace_helper(common_dir, shell_command)

    def create_workspace_backup(self, source_dir: Path, backup_dir: Path) -> None:
        """Create a backup copy of a workspace before destructive operations."""
        self.copy_workspace(source_dir, backup_dir)

    def restore_workspace_backup(self, backup_dir: Path, target_dir: Path) -> None:
        """Restore a workspace from a backup copy."""
        self.remove_workspace(target_dir)
        self.copy_workspace(backup_dir, target_dir)

    def remove_workspace(self, workspace_dir: Path) -> None:
        """Remove a workspace directory if it exists."""
        if not workspace_dir.exists():
            return
        parent_dir = workspace_dir.parent
        workspace_name = workspace_dir.name
        shell_command = f"rm -rf {shlex.quote(workspace_name)}"
        self._run_workspace_helper(parent_dir, shell_command)

    def _build_start_command(self, password: str, authorized_keys: list[str]) -> str:
        """Return the container start command that configures SSH."""
        authorized_keys_content = "\n".join(
            key.strip() for key in authorized_keys if key and key.strip()
        )
        encoded_keys = base64.b64encode(
            authorized_keys_content.encode("utf-8")
        ).decode("ascii")
        return (
            '/bin/bash -lc "mkdir -p /var/run/sshd; '
            'mkdir -p /root/.ssh; chmod 700 /root/.ssh; '
            f"printf %s {shlex.quote(encoded_keys)} | base64 -d > /root/.ssh/authorized_keys; "
            'chmod 600 /root/.ssh/authorized_keys; '
            f"echo root:{password} | chpasswd; "
            "(service ssh start || /etc/init.d/ssh start || /usr/sbin/sshd); "
            'trap : TERM INT; sleep infinity & wait"'
        )

    def _is_port_free(self, port: int) -> bool:
        """Return whether a host TCP port is free to use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return sock.connect_ex(("127.0.0.1", port)) != 0

    def _find_free_port(self) -> int:
        """Select a random free SSH port within the configured range."""
        candidates = list(range(PORT_RANGE[0], PORT_RANGE[1] + 1))
        random.shuffle(candidates)
        for port in candidates:
            if self._is_port_free(port):
                return port
        raise RuntimeError("No free SSH ports are available in the configured range.")

    def _is_port_conflict_error(self, exc: DockerException) -> bool:
        """Return whether a Docker error was caused by port allocation conflict."""
        message = str(exc).lower()
        return "port is already allocated" in message or "bind for" in message

    def list_local_images(self) -> list[dict[str, str]]:
        """List local Docker images as repository:tag pairs."""
        try:
            images = self._docker_client().images.list()
        except DockerException as exc:
            raise RuntimeError(f"Failed to list local Docker images: {exc}") from exc

        refs: set[str] = set()
        for image in images:
            for tag in image.tags:
                if not tag or "<none>" in tag:
                    continue
                refs.add(tag)

        return [{"image_ref": ref} for ref in sorted(refs)]

    def _container_by_name(self, container_name: str) -> Container:
        """Look up a managed container by name."""
        try:
            return self._docker_client().containers.get(container_name)
        except NotFound as exc:
            raise RuntimeError(f"Container {container_name} was not found.") from exc
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to inspect container {container_name}: {exc}"
            ) from exc

    def _ensure_image_available(self, image_ref: str) -> str:
        """Ensure the selected Docker image reference exists locally."""
        normalized = image_ref.strip()
        if not normalized:
            raise RuntimeError("Image selection cannot be empty.")
        try:
            self._docker_client().images.get(normalized)
            return normalized
        except ImageNotFound as exc:
            if normalized == "lab/pytorch:2.3-cuda12.1":
                raise RuntimeError(
                    "Docker image 'lab/pytorch:2.3-cuda12.1' was not found locally. "
                    "Build it first with: docker build -t lab/pytorch:2.3-cuda12.1 "
                    "-f docker/Dockerfile.pytorch docker"
                ) from exc
            raise RuntimeError(
                f"Docker image '{normalized}' was not found locally. "
                "Please build or retag this image first."
            ) from exc
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to inspect Docker image {normalized}: {exc}"
            ) from exc

    def ensure_image_available(self, image_ref: str) -> str:
        """Public wrapper for validating Docker image selection."""
        return self._ensure_image_available(image_ref)

    def is_snapshot_image(self, image_ref: str | None) -> bool:
        """Return whether one image ref belongs to the managed snapshot repository."""
        if not image_ref:
            return False
        return image_ref.startswith(f"{SNAPSHOT_IMAGE_REPOSITORY}:")

    def _snapshot_image_ref(self, container_name: str) -> str:
        """Return a new snapshot image reference for one container."""
        snapshot_tag = (
            f"{container_name}-"
            f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-"
            f"{secrets.token_hex(3)}"
        )
        return f"{SNAPSHOT_IMAGE_REPOSITORY}:{snapshot_tag}"

    def snapshot_container(self, container_name: str) -> str:
        """Commit the current container filesystem into one managed snapshot image."""
        snapshot_ref = self._snapshot_image_ref(container_name)
        _, snapshot_tag = snapshot_ref.split(":", 1)
        snapshot_client: docker.APIClient | None = None
        try:
            container = self._container_by_name(container_name)
            snapshot_client = docker.from_env(
                timeout=SNAPSHOT_COMMIT_TIMEOUT_SECONDS
            ).api
            snapshot_client.commit(
                container.id,
                repository=SNAPSHOT_IMAGE_REPOSITORY,
                tag=snapshot_tag,
                pause=False,
            )
            self._ensure_image_available(snapshot_ref)
            return snapshot_ref
        except requests_exceptions.ReadTimeout as exc:
            raise RuntimeError(
                "Failed to snapshot container "
                f"{container_name}: Docker commit exceeded "
                f"{SNAPSHOT_COMMIT_TIMEOUT_SECONDS}s. "
                "Increase SNAPSHOT_COMMIT_TIMEOUT_SECONDS if this instance has a large filesystem."
            ) from exc
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to snapshot container {container_name}: {exc}"
            ) from exc
        finally:
            if snapshot_client is not None:
                try:
                    snapshot_client.close()
                except Exception:
                    pass

    def update_container_resources(
        self,
        container_name: str,
        *,
        memory_gb: int,
        cpu_cores: int,
    ) -> None:
        """Update one existing container's memory and CPU limits in place."""
        cpu_period = 100_000
        cpu_quota = max(1, int(cpu_cores)) * cpu_period
        try:
            self._container_by_name(container_name).update(
                mem_limit=f"{memory_gb}g",
                memswap_limit=f"{memory_gb}g",
                cpu_period=cpu_period,
                cpu_quota=cpu_quota,
            )
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to update resources for container {container_name}: {exc}"
            ) from exc

    def remove_image(self, image_ref: str) -> None:
        """Remove one local Docker image by reference."""
        normalized = image_ref.strip()
        if not normalized:
            return
        try:
            self._docker_client().images.remove(normalized, force=True)
        except ImageNotFound:
            return
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to remove Docker image {normalized}: {exc}"
            ) from exc

    def create_container(
        self,
        username: str,
        gpu_indices: list[int],
        memory_gb: int,
        cpu_cores: int,
        image_name: str,
        container_name: str,
        workspace_dir: Path | None = None,
        authorized_keys: list[str] | None = None,
    ) -> dict[str, int | str]:
        """Create and start a GPU-enabled user container."""
        image_ref = self._ensure_image_available(image_name)
        ssh_password = self._generate_password()
        workspace_path = workspace_dir or self.get_instance_workspace_dir(
            username, container_name
        )
        container: Container | None = None
        for attempt in range(10):
            with self.port_lock:
                ssh_port = self._find_free_port()
                client = self._docker_client()
                try:
                    run_kwargs: dict[str, Any] = {
                        "name": container_name,
                        "detach": True,
                        "tty": True,
                        "stdin_open": True,
                        "command": self._build_start_command(
                            ssh_password,
                            authorized_keys or [],
                        ),
                        "ports": {"22/tcp": ssh_port},
                        "volumes": {
                            str(workspace_path): {
                                "bind": "/root/workspace",
                                "mode": "rw",
                            }
                        },
                        "mem_limit": f"{memory_gb}g",
                        "memswap_limit": f"{memory_gb}g",
                        "shm_size": f"{max(1, memory_gb // 2)}g",
                        "nano_cpus": cpu_cores * 1_000_000_000,
                        "pids_limit": DEFAULT_PIDS_LIMIT,
                    }
                    if gpu_indices:
                        run_kwargs["device_requests"] = [
                            DeviceRequest(
                                device_ids=[str(index) for index in gpu_indices],
                                capabilities=[["gpu"]],
                            )
                        ]

                    started_container = client.containers.run(image_ref, **run_kwargs)
                    container = started_container
                    if container is None:
                        raise RuntimeError("Docker did not return a container handle.")
                    container.reload()

                    # 更新 FRP 配置，将容器 SSH 端口穿透到 VPS
                    try:
                        self.frp_manager.add_container(container_name, ssh_port)
                    except Exception as exc:
                        LOGGER.warning(
                            "Failed to update FRP config for %s: %s",
                            container_name,
                            exc,
                        )

                    return {
                        "container_id": str(container.id),
                        "ssh_port": ssh_port,
                        "ssh_password": ssh_password,
                    }
                except DockerException as exc:
                    if container is not None:
                        try:
                            container.remove(force=True)
                        except DockerException:
                            LOGGER.warning(
                                "Failed to clean up partially created container %s",
                                container_name,
                            )
                        finally:
                            container = None
                    if self._is_port_conflict_error(exc) and attempt < 9:
                        LOGGER.warning(
                            "Port %s conflicted while creating %s, retrying",
                            ssh_port,
                            container_name,
                        )
                        continue
                    raise RuntimeError(
                        f"Failed to create Docker container: {exc}"
                    ) from exc
                except RuntimeError:
                    raise
        raise RuntimeError("Failed to allocate a usable SSH port for the container.")

    def stop_container(self, container_name: str) -> None:
        """Stop a running container."""
        try:
            self._container_by_name(container_name).stop(timeout=10)
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to stop container {container_name}: {exc}"
            ) from exc

    def restart_container(self, container_name: str) -> None:
        """Restart a stopped or running container."""
        try:
            self._container_by_name(container_name).restart(timeout=10)
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to restart container {container_name}: {exc}"
            ) from exc

    def remove_container(self, container_name: str) -> None:
        """Remove a container forcefully."""
        try:
            self._container_by_name(container_name).remove(force=True)

            # 移除 FRP 配置
            try:
                self.frp_manager.remove_container(container_name)
            except Exception as exc:
                LOGGER.warning(
                    "Failed to remove FRP config for %s: %s", container_name, exc
                )
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to remove container {container_name}: {exc}"
            ) from exc

    def get_logs(self, container_name: str, tail: int = 100) -> str:
        """Return the most recent container logs."""
        try:
            data = self._container_by_name(container_name).logs(tail=tail)
            return data.decode("utf-8", errors="replace")
        except DockerException as exc:
            raise RuntimeError(
                f"Failed to read logs for {container_name}: {exc}"
            ) from exc

    def get_container_status(self, container_name: str) -> str:
        """Return Docker's current status string for a container."""
        try:
            container = self._container_by_name(container_name)
            container.reload()
            return container.status
        except RuntimeError:
            return "missing"

    def list_managed_containers(self) -> list[Container]:
        """Return all containers created by this service naming convention."""
        try:
            containers = self._docker_client().containers.list(all=True)
        except DockerException as exc:
            raise RuntimeError(f"Failed to list Docker containers: {exc}") from exc
        return [
            container
            for container in containers
            if container.name and container.name.startswith("gpu_user_")
        ]

    def sync_frp_config(self) -> bool:
        """同步所有运行中容器的 FRP 配置（用于启动时）."""
        try:
            containers = self.list_managed_containers()
            return self.frp_manager.sync_with_docker(containers)
        except Exception as exc:
            LOGGER.error("Failed to sync FRP config: %s", exc)
            return False
