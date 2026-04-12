"""Background tasks that keep database and runtime state aligned."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session, sessionmaker

from config import (
    BACKUP_RETENTION_MINUTES,
    DATA_DIR,
    FALLBACK_DATA_DIR,
    ORPHAN_CONTAINER_GRACE_MINUTES,
)
from container_manager import ContainerManager
from gpu_manager import GPUManager
from models import GPUAllocation, Instance

LOGGER = logging.getLogger(__name__)


class InstanceScheduler:
    """Run periodic maintenance jobs for managed instances."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        container_manager: ContainerManager,
        gpu_manager: GPUManager,
    ) -> None:
        """Initialize the scheduler and register recurring jobs."""
        self.session_factory = session_factory
        self.container_manager = container_manager
        self.gpu_manager = gpu_manager
        self.scheduler = BackgroundScheduler()
        self.scheduler.add_job(
            self.sync_instance_statuses, "interval", seconds=60, id="sync_statuses"
        )
        self.scheduler.add_job(
            self.stop_expired_instances, "interval", minutes=5, id="stop_expired"
        )
        self.scheduler.add_job(
            self.reconcile_gpu_allocations, "interval", minutes=10, id="reconcile_gpus"
        )
        self.scheduler.add_job(
            self.cleanup_stale_backups, "interval", minutes=10, id="cleanup_backups"
        )
        self.scheduler.add_job(
            self.cleanup_orphan_containers, "interval", minutes=5, id="cleanup_orphans"
        )

    def start(self) -> None:
        """Start the background scheduler if not already running."""
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        """Stop the background scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def sync_instance_statuses(self) -> None:
        """Sync instance status values with actual Docker container state."""
        db = self.session_factory()
        try:
            instances = db.query(Instance).all()
            for instance in instances:
                instance_obj = cast(Any, instance)
                actual_status = self.container_manager.get_container_status(
                    str(instance_obj.container_name)
                )
                if actual_status == "running":
                    instance_obj.status = "running"
                    instance_obj.stopped_at = None
                    if instance_obj.expire_at is None:
                        hours = int(getattr(instance_obj, "auto_stop_hours", 6) or 6)
                        hours = max(1, min(72, hours))
                        instance_obj.expire_at = datetime.utcnow() + timedelta(
                            hours=hours
                        )
                elif actual_status in {"exited", "created", "paused"}:
                    self.gpu_manager.release(str(instance_obj.container_name), db)
                    instance_obj.status = "stopped"
                    instance_obj.stopped_at = (
                        instance_obj.stopped_at or datetime.utcnow()
                    )
                    instance_obj.expire_at = None
                elif actual_status == "missing":
                    self.gpu_manager.release(str(instance_obj.container_name), db)
                    instance_obj.status = "error"
                    instance_obj.stopped_at = (
                        instance_obj.stopped_at or datetime.utcnow()
                    )
                    instance_obj.expire_at = None
            db.commit()
        except Exception as exc:
            db.rollback()
            LOGGER.exception("Failed to sync instance statuses: %s", exc)
        finally:
            db.close()

    def stop_expired_instances(self) -> None:
        """Stop any running instances whose auto-stop deadline has passed."""
        db = self.session_factory()
        now = datetime.utcnow()
        try:
            expired_instances = (
                db.query(Instance)
                .filter(
                    Instance.status == "running",
                    Instance.expire_at.isnot(None),
                    Instance.expire_at <= now,
                )
                .all()
            )
            for instance in expired_instances:
                instance_obj = cast(Any, instance)
                try:
                    self.container_manager.stop_container(
                        str(instance_obj.container_name)
                    )
                    self.gpu_manager.release(str(instance_obj.container_name), db)
                    instance_obj.status = "stopped"
                    instance_obj.stopped_at = now
                    instance_obj.expire_at = None
                except Exception as exc:
                    instance_obj.status = "error"
                    LOGGER.exception(
                        "Failed to auto-stop instance %s: %s",
                        instance_obj.container_name,
                        exc,
                    )
            db.commit()
        except Exception as exc:
            db.rollback()
            LOGGER.exception("Failed to auto-stop instances: %s", exc)
        finally:
            db.close()

    def reconcile_gpu_allocations(self) -> None:
        """Ensure GPU allocation rows match currently running instances."""
        db = self.session_factory()
        try:
            running_instances = (
                db.query(Instance).filter(Instance.status == "running").all()
            )
            expected = {
                (gpu_index, instance.id)
                for instance in running_instances
                for gpu_index in instance.gpu_indices
            }
            current_rows = db.query(GPUAllocation).all()
            current = {(row.gpu_index, row.instance_id) for row in current_rows}

            for row in current_rows:
                if (row.gpu_index, row.instance_id) not in expected:
                    db.delete(row)

            for gpu_index, instance_id in expected - current:
                db.add(GPUAllocation(gpu_index=gpu_index, instance_id=instance_id))

            db.commit()
        except Exception as exc:
            db.rollback()
            LOGGER.exception("Failed to reconcile GPU allocations: %s", exc)
        finally:
            db.close()

    def cleanup_stale_backups(self) -> None:
        """Remove stale workspace backup directories left behind by restarts."""
        cutoff = datetime.utcnow().timestamp() - (BACKUP_RETENTION_MINUTES * 60)
        roots = {Path(DATA_DIR), Path(FALLBACK_DATA_DIR)}
        for root in roots:
            if not root.exists():
                continue
            try:
                for user_dir in root.iterdir():
                    if not user_dir.is_dir():
                        continue
                    for backup_dir in user_dir.glob("*_backup_*"):
                        if not backup_dir.is_dir():
                            continue
                        if backup_dir.stat().st_mtime > cutoff:
                            continue
                        try:
                            self.container_manager.remove_workspace(backup_dir)
                            LOGGER.info("Removed stale backup %s", backup_dir)
                        except RuntimeError as exc:
                            LOGGER.warning(
                                "Failed to remove stale backup %s: %s",
                                backup_dir,
                                exc,
                            )
            except Exception as exc:
                LOGGER.warning("Failed to scan backup root %s: %s", root, exc)

    def cleanup_orphan_containers(self) -> None:
        """Remove managed containers that do not have a corresponding DB record."""
        db = self.session_factory()
        try:
            known_container_names = {
                str(cast(Any, instance).container_name)
                for instance in db.query(Instance).all()
            }
            cutoff = datetime.now(timezone.utc).timestamp() - (
                ORPHAN_CONTAINER_GRACE_MINUTES * 60
            )
            for container in self.container_manager.list_managed_containers():
                container_name = str(container.name)
                if container_name in known_container_names:
                    continue
                try:
                    container.reload()
                    created_raw = container.attrs.get("Created")
                    created_at = None
                    if isinstance(created_raw, str):
                        created_at = datetime.fromisoformat(
                            created_raw.replace("Z", "+00:00")
                        )
                    if created_at is not None and created_at.timestamp() > cutoff:
                        continue
                    self.container_manager.remove_container(container_name)
                    LOGGER.warning("Removed orphan container %s", container_name)
                except Exception as exc:
                    LOGGER.warning(
                        "Failed to remove orphan container %s: %s",
                        container_name,
                        exc,
                    )
        finally:
            db.close()
