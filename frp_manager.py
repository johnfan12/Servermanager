"""FRP 配置管理模块 — 动态管理容器端口穿透."""

from __future__ import annotations

import configparser
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any

from docker.models.containers import Container
from filelock import FileLock

from config import (
    FRP_CONFIG_DIR,
    FRP_CONFIG_FILE,
    FRP_CONTAINER_SK_PREFIX,
    FRP_ENABLED,
    FRP_SERVER_ADDR,
    FRP_SERVER_PORT,
    FRP_TOKEN,
)

LOGGER = logging.getLogger(__name__)


class FrpManager:
    """管理 frpc 配置，为每个容器创建 stcp 隧道."""

    def __init__(self) -> None:
        """初始化 FRP 管理器."""
        self.enabled = FRP_ENABLED
        self.config_file = Path(FRP_CONFIG_FILE)
        self.config_dir = Path(FRP_CONFIG_DIR)

        # 确保目录存在以创建锁文件
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.config_dir / "frp_config.lock"))

    def _generate_secret_key(self, container_name: str) -> str:
        """为容器生成唯一的 secret key."""
        # 使用容器名生成确定的 secret key
        hash_input = f"{FRP_CONTAINER_SK_PREFIX}:{container_name}:{FRP_TOKEN}"
        hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
        return f"{FRP_CONTAINER_SK_PREFIX}-{container_name}-{hash_value}"

    def _build_config(
        self, containers: list[dict[str, Any]]
    ) -> configparser.ConfigParser:
        """构建 frpc 配置."""
        config = configparser.ConfigParser()
        setattr(config, "optionxform", str)  # 保持大小写

        # 基础配置
        config["common"] = {
            "server_addr": FRP_SERVER_ADDR,
            "server_port": str(FRP_SERVER_PORT),
            "token": FRP_TOKEN,
        }

        # 为每个容器创建 stcp 隧道
        for container_info in containers:
            name = container_info.get("name", "")
            ssh_port = container_info.get("ssh_port", 0)

            if not name or not ssh_port:
                continue

            section_name = f"container-{name}"
            secret_key = self._generate_secret_key(name)

            config[section_name] = {
                "type": "stcp",
                "local_ip": "127.0.0.1",
                "local_port": str(ssh_port),
                "sk": secret_key,
            }

        return config

    def update_config(self, containers: list[dict[str, Any]]) -> bool:
        """更新 frpc 配置文件并热重载."""
        if not self.enabled:
            LOGGER.info("FRP is disabled, skipping config update")
            return True

        try:
            # 确保配置目录存在
            self.config_dir.mkdir(parents=True, exist_ok=True)

            # 生成配置
            config = self._build_config(containers)

            # 写入临时文件，然后原子替换
            temp_file = self.config_file.with_suffix(".tmp")
            with open(temp_file, "w") as f:
                config.write(f)

            temp_file.replace(self.config_file)

            LOGGER.info(
                "Updated frpc config with %d containers: %s",
                len(containers),
                [c.get("name") for c in containers],
            )

            # 尝试热重载
            self._reload_frpc()

            return True

        except Exception as exc:
            LOGGER.error("Failed to update frpc config: %s", exc)
            return False

    def _reload_frpc(self) -> None:
        """热重载 frpc 配置."""
        try:
            result = subprocess.run(
                ["systemctl", "restart", "frpc-containers"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                LOGGER.info("Restarted frpc-containers via systemctl")
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        try:
            result = subprocess.run(
                ["systemctl", "reload", "frpc-containers"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                LOGGER.info("Restart unavailable; reloaded frpc-containers instead")
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        try:
            result = subprocess.run(
                ["pgrep", "-f", "frpc.*containers"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split("\n")[0]
                subprocess.run(["kill", "-HUP", pid], check=False)
                LOGGER.info(
                    "Systemctl unavailable; sent HUP to frpc-containers pid %s", pid
                )
                return
        except Exception as exc:
            LOGGER.warning("Failed to reload frpc via HUP: %s", exc)

        LOGGER.warning(
            "Could not reload or restart frpc automatically. "
            "Please ensure 'frpc-containers' can be managed via systemctl."
        )

    def get_container_secret(self, container_name: str) -> str:
        """获取容器的 secret key（供 VPS 使用）."""
        return self._generate_secret_key(container_name)

    def add_container(self, container_name: str, ssh_port: int) -> bool:
        """添加单个容器的 FRP 配置."""
        with self.lock:
            # 读取现有配置中的容器列表
            containers = self._load_existing_containers()

            # 添加或更新
            existing = False
            for c in containers:
                if c.get("name") == container_name:
                    c["ssh_port"] = ssh_port
                    existing = True
                    break

            if not existing:
                containers.append({"name": container_name, "ssh_port": ssh_port})

            return self.update_config(containers)

    def remove_container(self, container_name: str) -> bool:
        """移除单个容器的 FRP 配置."""
        with self.lock:
            containers = self._load_existing_containers()
            containers = [c for c in containers if c.get("name") != container_name]
            return self.update_config(containers)

    def _load_existing_containers(self) -> list[dict[str, Any]]:
        """从现有配置加载容器列表."""
        containers = []

        if not self.config_file.exists():
            return containers

        try:
            config = configparser.ConfigParser()
            config.read(self.config_file)

            for section in config.sections():
                if section.startswith("container-"):
                    name = section.replace("container-", "")
                    port = config.getint(section, "local_port", fallback=0)
                    if name and port:
                        containers.append({"name": name, "ssh_port": port})

        except Exception as exc:
            LOGGER.warning("Failed to load existing frpc config: %s", exc)

        return containers

    def sync_with_docker(
        self,
        docker_containers: list[Container],
    ) -> bool:
        """与 Docker 容器状态同步 FRP 配置."""
        containers = []

        for container in docker_containers:
            container_name = container.name or ""
            if not container_name.startswith("gpu_user_"):
                continue

            # 获取 SSH 端口映射
            ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            ssh_bindings = ports.get("22/tcp", [])

            if ssh_bindings:
                host_port = ssh_bindings[0].get("HostPort")
                if host_port:
                    containers.append(
                        {
                            "name": container_name,
                            "ssh_port": int(host_port),
                        }
                    )

        with self.lock:
            return self.update_config(containers)
