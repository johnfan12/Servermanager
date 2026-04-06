"""FRP config manager - per-container isolated frpc services."""

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
    FRP_API_CONFIG_FILE,
    FRP_API_ENABLED,
    FRP_API_LOCAL_PORT,
    FRP_API_PROXY_NAME,
    FRP_API_REMOTE_PORT,
    FRP_CONFIG_DIR,
    FRP_CONTAINER_CONFIG_DIR,
    FRP_CONTAINER_SK_PREFIX,
    FRP_ENABLED,
    FRP_SERVER_ADDR,
    FRP_SERVER_PORT,
    FRP_TOKEN,
    LEGACY_FRP_CONFIG_FILE,
)

LOGGER = logging.getLogger(__name__)


class FrpManager:
    """Manage per-container stcp server services."""

    def __init__(self) -> None:
        self.enabled = FRP_ENABLED
        self.config_dir = Path(FRP_CONFIG_DIR)
        self.instance_config_dir = Path(FRP_CONTAINER_CONFIG_DIR)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.instance_config_dir.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.config_dir / "frp_config.lock"))

    def _generate_secret_key(self, container_name: str) -> str:
        hash_input = f"{FRP_CONTAINER_SK_PREFIX}:{container_name}:{FRP_TOKEN}"
        hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
        return f"{FRP_CONTAINER_SK_PREFIX}-{container_name}-{hash_value}"

    def _instance_config_path(self, container_name: str) -> Path:
        return self.instance_config_dir / f"{container_name}.ini"

    def _instance_service_name(self, container_name: str) -> str:
        return f"frpc-container@{container_name}.service"

    def _api_config_path(self) -> Path:
        return Path(FRP_API_CONFIG_FILE)

    def _run_systemctl(self, action: str, service_name: str, timeout: int = 10) -> bool:
        commands = [
            ["sudo", "-n", "systemctl", action, service_name],
            ["systemctl", action, service_name],
        ]
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

            if result.returncode == 0:
                LOGGER.info(
                    "Ran '%s' for %s via command: %s",
                    action,
                    service_name,
                    " ".join(command),
                )
                return True

            error_text = (result.stderr or result.stdout or "").strip()
            if error_text:
                LOGGER.warning(
                    "Failed '%s' for %s via '%s': %s",
                    action,
                    service_name,
                    " ".join(command),
                    error_text,
                )
        return False

    def _is_service_active(self, service_name: str) -> bool:
        commands = [
            ["sudo", "-n", "systemctl", "is-active", "--quiet", service_name],
            ["systemctl", "is-active", "--quiet", service_name],
        ]
        for command in commands:
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=5
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
            if result.returncode == 0:
                return True
        return False

    def _build_instance_config(
        self,
        container_name: str,
        ssh_port: int,
    ) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        setattr(config, "optionxform", str)
        config["common"] = {
            "server_addr": FRP_SERVER_ADDR,
            "server_port": str(FRP_SERVER_PORT),
            "token": FRP_TOKEN,
        }
        config[f"container-{container_name}"] = {
            "type": "stcp",
            "local_ip": "127.0.0.1",
            "local_port": str(ssh_port),
            "sk": self._generate_secret_key(container_name),
        }
        return config

    def _build_api_config(self) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        setattr(config, "optionxform", str)
        config["common"] = {
            "server_addr": FRP_SERVER_ADDR,
            "server_port": str(FRP_SERVER_PORT),
            "token": FRP_TOKEN,
        }
        config[FRP_API_PROXY_NAME] = {
            "type": "tcp",
            "local_ip": "127.0.0.1",
            "local_port": str(FRP_API_LOCAL_PORT),
            "remote_port": str(FRP_API_REMOTE_PORT),
        }
        return config

    def _render_config(self, config: configparser.ConfigParser) -> str:
        lines: list[str] = []
        for section in config.sections():
            lines.append(f"[{section}]")
            for key, value in config.items(section):
                lines.append(f"{key} = {value}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _write_instance_config(
        self,
        container_name: str,
        ssh_port: int,
    ) -> bool:
        config_path = self._instance_config_path(container_name)
        config = self._build_instance_config(container_name, ssh_port)
        rendered = self._render_config(config)

        existing = ""
        if config_path.exists():
            existing = config_path.read_text()
        if existing == rendered:
            return False

        temp_file = config_path.with_suffix(".tmp")
        temp_file.write_text(rendered)
        temp_file.replace(config_path)
        return True

    def sync_api_client_config(self) -> bool:
        """Keep /etc/frp/frpc-api.ini aligned with .env-driven settings."""
        if not self.enabled or not FRP_API_ENABLED:
            LOGGER.info("FRP API client is disabled; skip API config sync")
            return True

        config_path = self._api_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = self._render_config(self._build_api_config())
        existing = config_path.read_text() if config_path.exists() else ""
        if existing == rendered:
            LOGGER.info("FRP API config already up to date: %s", config_path)
            return True

        temp_file = config_path.with_suffix(".tmp")
        temp_file.write_text(rendered)
        temp_file.replace(config_path)
        LOGGER.info("Synced FRP API config from environment: %s", config_path)

        service_name = "frpc-api.service"
        if self._is_service_active(service_name):
            if not self._run_systemctl("restart", service_name):
                LOGGER.warning(
                    "FRP API config changed, but failed to restart %s",
                    service_name,
                )
                return False
        return True

    def _load_existing_containers(self) -> list[dict[str, Any]]:
        containers: list[dict[str, Any]] = []
        if self.instance_config_dir.exists():
            for cfg_file in sorted(self.instance_config_dir.glob("*.ini")):
                try:
                    config = configparser.ConfigParser()
                    config.read(cfg_file)
                    for section in config.sections():
                        if not section.startswith("container-"):
                            continue
                        name = section.removeprefix("container-")
                        port = config.getint(section, "local_port", fallback=0)
                        if name and port:
                            containers.append({"name": name, "ssh_port": port})
                            break
                except Exception as exc:
                    LOGGER.warning("Failed to load FRP config %s: %s", cfg_file, exc)

        if containers:
            return containers

        # Migration-only fallback for nodes that still have the removed
        # aggregate-mode frpc-containers.ini on disk.
        legacy_file = Path(LEGACY_FRP_CONFIG_FILE)
        if legacy_file.exists():
            try:
                legacy_cfg = configparser.ConfigParser()
                legacy_cfg.read(legacy_file)
                for section in legacy_cfg.sections():
                    if not section.startswith("container-"):
                        continue
                    name = section.removeprefix("container-")
                    port = legacy_cfg.getint(section, "local_port", fallback=0)
                    if name and port:
                        containers.append({"name": name, "ssh_port": port})
            except Exception as exc:
                LOGGER.warning(
                    "Failed to load legacy FRP config %s: %s", legacy_file, exc
                )
        return containers

    def _remove_legacy_containers(self, container_names: set[str]) -> bool:
        """Remove container sections from legacy FRP config to prevent ghost recovery."""
        if not container_names:
            return True

        legacy_file = Path(LEGACY_FRP_CONFIG_FILE)
        if not legacy_file.exists():
            return True

        try:
            legacy_cfg = configparser.ConfigParser()
            legacy_cfg.read(legacy_file)
            removed = False

            for section in list(legacy_cfg.sections()):
                if not section.startswith("container-"):
                    continue
                name = section.removeprefix("container-")
                if name in container_names:
                    legacy_cfg.remove_section(section)
                    removed = True

            if not removed:
                return True

            if not legacy_cfg.sections():
                legacy_file.unlink(missing_ok=True)
                return True

            temp_file = legacy_file.with_suffix(".tmp")
            with temp_file.open("w") as fh:
                legacy_cfg.write(fh)
            temp_file.replace(legacy_file)
            return True
        except Exception as exc:
            LOGGER.warning("Failed to cleanup legacy FRP config %s: %s", legacy_file, exc)
            return False

    def get_ready_containers(self) -> list[dict[str, Any]]:
        """Load containers and ensure corresponding frpc-container@ services are active."""
        containers = self._load_existing_containers()
        ready: list[dict[str, Any]] = []
        for container in containers:
            name = str(container.get("name", ""))
            port = int(container.get("ssh_port", 0))
            if not name or not port:
                continue

            service_name = self._instance_service_name(name)
            if not self._is_service_active(service_name):
                self._run_systemctl("start", service_name)

            if self._is_service_active(service_name):
                ready.append({"name": name, "ssh_port": port})
            else:
                LOGGER.warning(
                    "Skip container %s: FRP service %s is not active",
                    name,
                    service_name,
                )

        return ready

    def _reconcile(self, containers: list[dict[str, Any]]) -> bool:
        desired_map = {
            str(c.get("name", "")): int(c.get("ssh_port", 0))
            for c in containers
            if c.get("name") and c.get("ssh_port")
        }
        existing = {
            c["name"]: int(c["ssh_port"])
            for c in self._load_existing_containers()
            if c.get("name") and c.get("ssh_port")
        }

        success = True

        for name, port in sorted(desired_map.items()):
            changed = self._write_instance_config(name, port)
            service_name = self._instance_service_name(name)

            if name not in existing:
                success = self._run_systemctl("start", service_name) and success
                continue

            if changed or existing.get(name) != port:
                success = self._run_systemctl("restart", service_name) and success
                continue

            if not self._is_service_active(service_name):
                LOGGER.warning(
                    "FRP service %s is inactive, starting it",
                    service_name,
                )
                success = self._run_systemctl("start", service_name) and success

        stale_names = sorted(set(existing.keys()) - set(desired_map.keys()))
        for name in stale_names:
            service_name = self._instance_service_name(name)
            self._run_systemctl("stop", service_name)
            config_path = self._instance_config_path(name)
            try:
                if config_path.exists():
                    config_path.unlink()
            except Exception as exc:
                success = False
                LOGGER.warning(
                    "Failed to delete stale FRP config %s: %s", config_path, exc
                )

        if stale_names:
            success = self._remove_legacy_containers(set(stale_names)) and success

        return success

    def update_config(self, containers: list[dict[str, Any]]) -> bool:
        if not self.enabled:
            LOGGER.info("FRP is disabled, skipping config update")
            return True
        try:
            self.instance_config_dir.mkdir(parents=True, exist_ok=True)
            message = "Reconciling per-container FRP services for %d containers: %s"
            args = (len(containers), [c.get("name") for c in containers])
            if containers:
                LOGGER.info(message, *args)
            else:
                LOGGER.debug(message, *args)
            return self._reconcile(containers)
        except Exception as exc:
            LOGGER.error("Failed to reconcile per-container FRP services: %s", exc)
            return False

    def get_container_secret(self, container_name: str) -> str:
        return self._generate_secret_key(container_name)

    def add_container(self, container_name: str, ssh_port: int) -> bool:
        with self.lock:
            containers = self._load_existing_containers()
            existing = False
            for container in containers:
                if container.get("name") == container_name:
                    container["ssh_port"] = ssh_port
                    existing = True
                    break
            if not existing:
                containers.append({"name": container_name, "ssh_port": ssh_port})
            return self.update_config(containers)

    def remove_container(self, container_name: str) -> bool:
        with self.lock:
            containers = self._load_existing_containers()
            containers = [c for c in containers if c.get("name") != container_name]
            return self.update_config(containers)

    def sync_with_docker(self, docker_containers: list[Container]) -> bool:
        containers: list[dict[str, Any]] = []
        for container in docker_containers:
            container_name = container.name or ""
            if not container_name.startswith("gpu_user_"):
                continue

            ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            ssh_bindings = ports.get("22/tcp", [])
            if ssh_bindings:
                host_port = ssh_bindings[0].get("HostPort")
                if host_port:
                    containers.append(
                        {"name": container_name, "ssh_port": int(host_port)}
                    )

        with self.lock:
            return self.update_config(containers)
