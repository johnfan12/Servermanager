"""FastAPI application for managing multi-user GPU container instances."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from auth import (
    create_access_token,
    ensure_default_admin,
    get_admin_user,
    get_current_user,
    hash_password,
    verify_password,
)
from config import (
    ALLOW_REGISTER,
    AVAILABLE_IMAGES,
    CORS_ALLOW_CREDENTIALS,
    CORS_ALLOW_ORIGINS,
    ENV,
    INTERNAL_SERVICE_TOKEN,
    JWT_SECRET,
    LOG_DIR,
    SERVER_IP,
)
from container_manager import ContainerManager
from database import SessionLocal, get_db, init_db
from gpu_manager import GPUManager
from models import GPUAllocation, Instance, User
from scheduler import InstanceScheduler

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log"),
        logging.StreamHandler(),
    ],
)
LOGGER = logging.getLogger(__name__)


def _unique_name(prefix: str) -> str:
    """Return a collision-resistant resource name."""
    return f"{prefix}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{secrets.token_hex(3)}"


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Initialize and tear down persistent services around app lifetime."""
    del application
    if JWT_SECRET == "change-this-secret":
        LOGGER.warning(
            "JWT_SECRET is using the default insecure value. Set environment variable JWT_SECRET in production."
        )
    if ENV == "prod":
        LOGGER.info("Running in production mode with strict config checks")
    init_db()
    db = SessionLocal()
    try:
        ensure_default_admin(db)
    finally:
        db.close()
    scheduler_service.start()

    # 启动时同步 FRP 配置
    try:
        container_manager.sync_frp_config()
    except Exception as exc:
        LOGGER.warning("Failed to sync FRP config on startup: %s", exc)

    try:
        yield
    finally:
        scheduler_service.shutdown()


app = FastAPI(title="GPU Server Manager", lifespan=lifespan)

# cluster_manager 适配：添加 CORS 中间件，允许来自 cluster_manager 前端域名的跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CORS_ALLOW_ORIGINS),
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

if CORS_ALLOW_CREDENTIALS and "*" in CORS_ALLOW_ORIGINS:
    raise RuntimeError(
        "Invalid CORS configuration: cannot use wildcard origin when credentials are enabled."
    )

container_manager = ContainerManager()
gpu_manager = GPUManager(SessionLocal)
scheduler_service = InstanceScheduler(SessionLocal, container_manager, gpu_manager)


class LoginRequest(BaseModel):
    """Request body for user login."""

    username: str
    password: str


class RegisterRequest(BaseModel):
    """Request body for user registration."""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)
    email: str


class InstanceCreateRequest(BaseModel):
    """Request body for creating a new instance."""

    num_gpus: int = Field(ge=0)
    memory_gb: int = Field(ge=8)
    image: str
    expire_hours: int | None = Field(default=None, ge=1)


class QuotaUpdateRequest(BaseModel):
    """Request body for quota updates."""

    quota_gpu: int = Field(ge=1)
    quota_memory_gb: int = Field(ge=8)
    quota_max_instances: int = Field(ge=1)


class InstanceRebuildRequest(BaseModel):
    """Request body for rebuilding an instance with a new GPU count."""

    num_gpus: int = Field(ge=0)
    memory_gb: int = Field(ge=8)


def _serialize_instance(instance: Instance) -> dict[str, Any]:
    """Convert an instance ORM object into an API response payload.

    cluster_manager 适配：返回字段包含
    id, container_name, gpu_indices, memory_gb, image_name, status,
    ssh_port, ssh_password, expire_at（无到期时间时为 null）, created_at
    """
    instance_obj = cast(Any, instance)
    expire_seconds = None
    expire_at = instance_obj.expire_at
    stopped_at = instance_obj.stopped_at
    ssh_port = instance_obj.ssh_port
    if expire_at is not None:
        expire_seconds = int((expire_at - datetime.utcnow()).total_seconds())
    return {
        "id": instance_obj.id,
        "user_id": instance_obj.user_id,
        "username": instance_obj.user.username
        if instance_obj.user is not None
        else None,
        "container_name": instance_obj.container_name,
        "container_id": instance_obj.container_id,
        "gpu_indices": instance_obj.gpu_indices,
        "memory_gb": instance_obj.memory_gb,
        "cpu_cores": instance_obj.cpu_cores,
        "ssh_port": ssh_port,
        "ssh_password": instance_obj.ssh_password,
        "ssh_command": f"ssh -p {ssh_port} root@{SERVER_IP}"
        if ssh_port is not None
        else None,
        "vps_access": instance_obj.vps_access,
        "image_name": instance_obj.image_name,
        "status": instance_obj.status,
        "created_at": instance_obj.created_at.isoformat(),
        "stopped_at": stopped_at.isoformat() if stopped_at is not None else None,
        "expire_at": expire_at.isoformat() if expire_at is not None else None,
        "expire_seconds": expire_seconds,
    }


def _get_running_usage(user: User) -> dict[str, int]:
    """Compute current quota usage from running instances only."""
    user_obj = cast(Any, user)
    running_instances = [
        instance
        for instance in user_obj.instances
        if cast(Any, instance).status == "running"
    ]
    return {
        "used_gpu": sum(
            len(cast(Any, instance).gpu_indices) for instance in running_instances
        ),
        "used_memory_gb": sum(
            cast(Any, instance).memory_gb for instance in running_instances
        ),
        "used_instances": len(running_instances),
    }


def _get_instance_for_user(db: Session, instance_id: int, user: User) -> Instance:
    """Return an instance if it belongs to the user, otherwise raise 404."""
    instance = (
        db.query(Instance)
        .options(joinedload(Instance.user))
        .filter(Instance.id == instance_id, Instance.user_id == user.id)
        .first()
    )
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found.")
    return instance


def _delete_instance(db: Session, instance: Instance) -> None:
    """Delete a container instance and clean related allocations."""
    instance_obj = cast(Any, instance)
    try:
        container_manager.remove_container(str(instance_obj.container_name))
    except RuntimeError as exc:
        if "not found" not in str(exc).lower():
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    gpu_manager.release(str(instance_obj.container_name), db)
    LOGGER.info(
        "Deleting instance %s for user %s",
        instance_obj.container_name,
        instance_obj.user.username,
    )
    db.delete(instance)
    db.commit()


def _schedule_backup_cleanup(backup_path: Path) -> None:
    """Delete a temporary backup directory after a short grace period."""

    async def _cleanup() -> None:
        await asyncio.sleep(300)
        try:
            container_manager.remove_workspace(backup_path)
            LOGGER.info("Removed temporary backup %s", backup_path)
        except RuntimeError as exc:
            LOGGER.warning("Failed to remove temporary backup %s: %s", backup_path, exc)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_cleanup())
    except RuntimeError:
        LOGGER.warning(
            "No running event loop available for delayed backup cleanup: %s",
            backup_path,
        )


def _clone_instance_record(
    db: Session,
    source_instance: Instance,
    user: User,
    container_name: str,
    gpu_indices: list[int],
    workspace_dir: Path,
) -> Instance:
    """Create a DB record and Docker container for a cloned instance."""
    source_obj = cast(Any, source_instance)
    user_obj = cast(Any, user)
    instance = Instance(
        user_id=user_obj.id,
        container_name=container_name,
        gpu_indices=gpu_indices,
        memory_gb=int(source_obj.memory_gb),
        cpu_cores=int(source_obj.cpu_cores),
        image_name=str(source_obj.image_name),
        status="error",
        expire_at=source_obj.expire_at,
    )
    db.add(instance)
    db.flush()

    for gpu_index in gpu_indices:
        db.add(GPUAllocation(gpu_index=gpu_index, instance_id=instance.id))

    container_info = container_manager.create_container(
        username=str(user_obj.username),
        gpu_indices=gpu_indices,
        memory_gb=int(source_obj.memory_gb),
        cpu_cores=int(source_obj.cpu_cores),
        image_name=str(source_obj.image_name),
        container_name=container_name,
        workspace_dir=workspace_dir,
    )
    instance_obj = cast(Any, instance)
    instance_obj.container_id = str(container_info["container_id"])
    instance_obj.ssh_port = int(container_info["ssh_port"])
    instance_obj.ssh_password = str(container_info["ssh_password"])
    instance_obj.status = "running"
    return instance


def _rebuild_instance_with_new_gpus(
    db: Session,
    instance: Instance,
    user: User,
    new_gpu_indices: list[int],
    new_memory_gb: int,
) -> Instance:
    """Rebuild an instance after config changes with backup protection."""
    instance_obj = cast(Any, instance)
    user_obj = cast(Any, user)
    original_gpu_indices = list(instance_obj.gpu_indices)
    original_memory_gb = int(instance_obj.memory_gb)
    original_cpu_cores = int(instance_obj.cpu_cores)
    source_workspace = container_manager.locate_instance_workspace_dir(
        str(user_obj.username), str(instance_obj.container_name)
    )
    backup_path = source_workspace.parent / _unique_name(f"{user_obj.username}_backup")
    target_workspace = container_manager.get_instance_workspace_dir(
        str(user_obj.username), str(instance_obj.container_name), create=False
    )

    container_manager.create_workspace_backup(source_workspace, backup_path)

    try:
        container_manager.remove_container(str(instance_obj.container_name))
    except RuntimeError as exc:
        if "not found" not in str(exc).lower():
            raise

    gpu_manager.release(str(instance_obj.container_name), db)

    try:
        container_manager.remove_workspace(target_workspace)
        container_manager.copy_workspace(backup_path, target_workspace)
        instance_obj.gpu_indices = list(new_gpu_indices)
        instance_obj.memory_gb = new_memory_gb
        instance_obj.cpu_cores = max(4, len(new_gpu_indices) * 8)
        for gpu_index in new_gpu_indices:
            db.add(GPUAllocation(gpu_index=gpu_index, instance_id=instance_obj.id))
        db.flush()
        container_info = container_manager.create_container(
            username=str(user_obj.username),
            gpu_indices=new_gpu_indices,
            memory_gb=new_memory_gb,
            cpu_cores=max(4, len(new_gpu_indices) * 8),
            image_name=str(instance_obj.image_name),
            container_name=str(instance_obj.container_name),
            workspace_dir=target_workspace,
        )
        instance_obj.container_id = str(container_info["container_id"])
        instance_obj.ssh_port = int(container_info["ssh_port"])
        instance_obj.ssh_password = str(container_info["ssh_password"])
        instance_obj.status = "running"
        instance_obj.stopped_at = None
        _schedule_backup_cleanup(backup_path)
        return instance
    except Exception as exc:
        try:
            container_manager.remove_container(str(instance_obj.container_name))
        except RuntimeError:
            pass
        container_manager.restore_workspace_backup(backup_path, target_workspace)
        instance_obj.gpu_indices = original_gpu_indices
        instance_obj.memory_gb = original_memory_gb
        instance_obj.cpu_cores = original_cpu_cores
        instance_obj.status = "error"
        LOGGER.exception(
            "Failed to rebuild instance %s: %s", instance_obj.container_name, exc
        )
        raise RuntimeError(
            f"Failed to rebuild instance {instance_obj.container_name}; original data was restored from backup."
        ) from exc


def _choose_rebuild_gpu_indices(
    db: Session,
    instance: Instance,
    requested_gpu_count: int,
) -> list[int]:
    """Choose GPUs for an instance rebuild, preferring current assignments."""
    instance_obj = cast(Any, instance)
    current_gpu_indices = [
        int(gpu_index) for gpu_index in list(instance_obj.gpu_indices)
    ]
    if requested_gpu_count == 0:
        return []

    statuses = gpu_manager.get_gpu_status(db)
    status_map: dict[int, dict[str, Any]] = {}
    for status in statuses:
        gpu_index = status.get("index")
        if isinstance(gpu_index, int):
            status_map[gpu_index] = status

    reusable = [
        gpu_index for gpu_index in current_gpu_indices if gpu_index in status_map
    ]
    selected = reusable[:requested_gpu_count]
    if len(selected) == requested_gpu_count:
        return selected

    idle_candidates = [
        gpu_index
        for gpu_index, status in status_map.items()
        if bool(status.get("is_idle")) and gpu_index not in selected
    ]
    needed = requested_gpu_count - len(selected)
    if len(idle_candidates) < needed:
        raise HTTPException(
            status_code=400,
            detail="Not enough available GPUs to rebuild this instance.",
        )
    selected.extend(idle_candidates[:needed])
    return selected


@app.get("/")
def index() -> FileResponse:
    """Serve the single-file management UI."""
    return FileResponse(Path("static") / "index.html")


@app.get("/api/meta")
def get_meta() -> dict[str, Any]:
    """Return frontend bootstrap metadata."""
    return {
        "server_ip": SERVER_IP,
        "available_images": AVAILABLE_IMAGES,
        "allow_register": ALLOW_REGISTER,
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Authenticate a user and return a JWT token."""
    user = (
        db.query(User)
        .options(joinedload(User.instances))
        .filter(User.username == payload.username)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    user_obj = cast(Any, user)
    if not verify_password(payload.password, str(user_obj.password_hash)):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    usage = _get_running_usage(user)
    return {
        "access_token": create_access_token(str(user_obj.username)),
        "token_type": "bearer",
        "user": {
            "id": user_obj.id,
            "username": user_obj.username,
            "email": user_obj.email,
            "is_admin": user_obj.is_admin,
            **usage,
            "quota_gpu": user_obj.quota_gpu,
            "quota_memory_gb": user_obj.quota_memory_gb,
            "quota_max_instances": user_obj.quota_max_instances,
        },
    }


@app.get("/api/me")
def get_me(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return current authenticated user profile for token-based bootstrap."""
    current_user_obj = cast(Any, current_user)
    usage = _get_running_usage(current_user)
    return {
        "id": current_user_obj.id,
        "username": current_user_obj.username,
        "email": current_user_obj.email,
        "is_admin": current_user_obj.is_admin,
        **usage,
        "quota_gpu": current_user_obj.quota_gpu,
        "quota_memory_gb": current_user_obj.quota_memory_gb,
        "quota_max_instances": current_user_obj.quota_max_instances,
    }


@app.post("/api/auth/register")
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    """Create a new user account when registration is enabled."""
    if not ALLOW_REGISTER:
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username is already in use.")
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email is already in use.")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        email=payload.email,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Username or email is already in use.",
        ) from exc
    return {"message": "Registration successful."}


@app.get("/api/instances")
def list_instances(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """List all instances owned by the current user."""
    instances = (
        db.query(Instance)
        .options(joinedload(Instance.user))
        .filter(Instance.user_id == current_user.id)
        .order_by(Instance.created_at.desc())
        .all()
    )
    return [_serialize_instance(instance) for instance in instances]


@app.post("/api/instances")
def create_instance(
    payload: InstanceCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a new GPU container instance for the current user."""
    current_user_obj = cast(Any, current_user)
    if payload.num_gpus not in {0, 1, 2, 4, 8}:
        raise HTTPException(
            status_code=400, detail="Supported GPU counts are 0, 1, 2, 4, or 8."
        )
    if payload.memory_gb % 8 != 0:
        raise HTTPException(
            status_code=400, detail="Memory must be allocated in 8 GB increments."
        )
    if payload.image not in AVAILABLE_IMAGES:
        raise HTTPException(status_code=400, detail="Unsupported image selection.")

    db.refresh(current_user)
    usage = _get_running_usage(current_user)
    if usage["used_instances"] + 1 > int(current_user_obj.quota_max_instances):
        raise HTTPException(status_code=400, detail="Instance quota exceeded.")
    if usage["used_gpu"] + payload.num_gpus > int(current_user_obj.quota_gpu):
        raise HTTPException(status_code=400, detail="GPU quota exceeded.")
    if usage["used_memory_gb"] + payload.memory_gb > int(
        current_user_obj.quota_memory_gb
    ):
        raise HTTPException(status_code=400, detail="Memory quota exceeded.")

    cpu_cores = max(4, payload.num_gpus * 8)
    expire_at = (
        datetime.utcnow() + timedelta(hours=payload.expire_hours)
        if payload.expire_hours is not None
        else None
    )
    container_name = _unique_name(f"gpu_user_{current_user_obj.username}")
    workspace_path = container_manager.get_instance_workspace_dir(
        str(current_user_obj.username), container_name
    )
    container_created = False

    with gpu_manager.locked_allocation():
        try:
            selected_gpus: list[int] = []
            if payload.num_gpus > 0:
                statuses = gpu_manager.get_gpu_status(db)
                idle_gpu_indices: list[int] = []
                for status in statuses:
                    if status.get("is_idle") is True:
                        gpu_index = status.get("index")
                        if isinstance(gpu_index, int):
                            idle_gpu_indices.append(gpu_index)
                if len(idle_gpu_indices) < payload.num_gpus:
                    raise HTTPException(
                        status_code=400, detail="Not enough idle GPUs are available."
                    )

                selected_gpus = idle_gpu_indices[: payload.num_gpus]
                gpu_manager.allocate(
                    current_user, selected_gpus, payload.memory_gb, cpu_cores, db
                )

            instance = Instance(
                user_id=current_user.id,
                container_name=container_name,
                gpu_indices=selected_gpus,
                memory_gb=payload.memory_gb,
                cpu_cores=cpu_cores,
                image_name=payload.image,
                status="error",
                expire_at=expire_at,
            )
            db.add(instance)
            db.flush()

            for gpu_index in selected_gpus:
                db.add(GPUAllocation(gpu_index=gpu_index, instance_id=instance.id))

            container_info = container_manager.create_container(
                username=str(current_user_obj.username),
                gpu_indices=selected_gpus,
                memory_gb=payload.memory_gb,
                cpu_cores=cpu_cores,
                image_name=payload.image,
                container_name=container_name,
                workspace_dir=workspace_path,
            )
            container_created = True
            instance_obj = cast(Any, instance)
            instance_obj.container_id = str(container_info["container_id"])
            instance_obj.ssh_port = int(container_info["ssh_port"])
            instance_obj.ssh_password = str(container_info["ssh_password"])
            instance_obj.status = "running"
            LOGGER.info(
                "Created instance %s for user %s",
                instance_obj.container_name,
                current_user_obj.username,
            )
            db.commit()
            db.refresh(instance)
            return _serialize_instance(instance)
        except HTTPException:
            if container_created:
                try:
                    container_manager.remove_container(container_name)
                except RuntimeError:
                    pass
                try:
                    container_manager.remove_workspace(workspace_path)
                except RuntimeError:
                    pass
            db.rollback()
            raise
        except ValueError as exc:
            if container_created:
                try:
                    container_manager.remove_container(container_name)
                except RuntimeError:
                    pass
                try:
                    container_manager.remove_workspace(workspace_path)
                except RuntimeError:
                    pass
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            if container_created:
                try:
                    container_manager.remove_container(container_name)
                except RuntimeError:
                    pass
                try:
                    container_manager.remove_workspace(workspace_path)
                except RuntimeError:
                    pass
            db.rollback()
            status_code = 503 if "Unable to connect to Docker" in str(exc) else 500
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except Exception as exc:
            if container_created:
                try:
                    container_manager.remove_container(container_name)
                except RuntimeError:
                    pass
                try:
                    container_manager.remove_workspace(workspace_path)
                except RuntimeError:
                    pass
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to create instance: {exc}"
            ) from exc


@app.delete("/api/instances/{instance_id}")
def delete_instance(
    instance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Delete one of the current user's instances."""
    instance = _get_instance_for_user(db, instance_id, current_user)
    _delete_instance(db, instance)
    return {"message": "Instance deleted."}


@app.post("/api/instances/{instance_id}/stop")
def stop_instance(
    instance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Stop a running instance owned by the current user."""
    instance = _get_instance_for_user(db, instance_id, current_user)
    instance_obj = cast(Any, instance)
    try:
        container_manager.stop_container(str(instance_obj.container_name))
        gpu_manager.release(str(instance_obj.container_name), db)
        instance_obj.status = "stopped"
        instance_obj.stopped_at = datetime.utcnow()
        db.commit()
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"message": "Instance stopped."}


@app.post("/api/instances/{instance_id}/restart")
def restart_instance(
    instance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Restart a stopped instance after validating its GPU reservation."""
    instance = _get_instance_for_user(db, instance_id, current_user)
    instance_obj = cast(Any, instance)
    with gpu_manager.locked_allocation():
        try:
            gpu_indices = list(instance_obj.gpu_indices)
            if gpu_indices:
                gpu_manager.allocate(
                    current_user,
                    gpu_indices,
                    int(instance_obj.memory_gb),
                    int(instance_obj.cpu_cores),
                    db,
                )
            for gpu_index in gpu_indices:
                exists = (
                    db.query(GPUAllocation)
                    .filter(
                        GPUAllocation.gpu_index == gpu_index,
                        GPUAllocation.instance_id == instance_obj.id,
                    )
                    .first()
                )
                if not exists:
                    db.add(
                        GPUAllocation(gpu_index=gpu_index, instance_id=instance_obj.id)
                    )
            container_manager.restart_container(str(instance_obj.container_name))
            instance_obj.status = "running"
            instance_obj.stopped_at = None
            db.commit()
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"message": "Instance restarted."}


@app.post("/api/instances/{instance_id}/clone")
def clone_instance(
    instance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Clone an instance, including its workspace data, into a new container."""
    source_instance = _get_instance_for_user(db, instance_id, current_user)
    source_obj = cast(Any, source_instance)
    current_user_obj = cast(Any, current_user)

    db.refresh(current_user)
    usage = _get_running_usage(current_user)
    source_gpu_count = len(list(source_obj.gpu_indices))
    if usage["used_instances"] + 1 > int(current_user_obj.quota_max_instances):
        raise HTTPException(status_code=400, detail="Instance quota exceeded.")
    if usage["used_gpu"] + source_gpu_count > int(current_user_obj.quota_gpu):
        raise HTTPException(status_code=400, detail="GPU quota exceeded for clone.")
    if usage["used_memory_gb"] + int(source_obj.memory_gb) > int(
        current_user_obj.quota_memory_gb
    ):
        raise HTTPException(status_code=400, detail="Memory quota exceeded for clone.")

    clone_name = _unique_name(f"gpu_user_{current_user_obj.username}_clone")
    source_workspace = container_manager.locate_instance_workspace_dir(
        str(current_user_obj.username), str(source_obj.container_name)
    )
    clone_workspace = container_manager.get_instance_workspace_dir(
        str(current_user_obj.username), clone_name, create=False
    )

    with gpu_manager.locked_allocation():
        try:
            selected_gpus: list[int] = []
            if source_gpu_count > 0:
                statuses = gpu_manager.get_gpu_status(db)
                idle_gpu_indices: list[int] = []
                for status in statuses:
                    if status.get("is_idle") is True:
                        gpu_index = status.get("index")
                        if isinstance(gpu_index, int):
                            idle_gpu_indices.append(gpu_index)
                if len(idle_gpu_indices) < source_gpu_count:
                    raise HTTPException(
                        status_code=400,
                        detail="Not enough idle GPUs are available for cloning.",
                    )
                selected_gpus = idle_gpu_indices[:source_gpu_count]
                gpu_manager.allocate(
                    current_user,
                    selected_gpus,
                    int(source_obj.memory_gb),
                    int(source_obj.cpu_cores),
                    db,
                )

            container_manager.copy_workspace(source_workspace, clone_workspace)
            clone_instance_obj = _clone_instance_record(
                db,
                source_instance,
                current_user,
                clone_name,
                selected_gpus,
                clone_workspace,
            )
            LOGGER.info(
                "Cloned instance %s to %s for user %s",
                source_obj.container_name,
                clone_name,
                current_user_obj.username,
            )
            db.commit()
            db.refresh(clone_instance_obj)
            return _serialize_instance(clone_instance_obj)
        except HTTPException:
            db.rollback()
            raise
        except (RuntimeError, ValueError) as exc:
            db.rollback()
            try:
                container_manager.remove_workspace(clone_workspace)
            except RuntimeError:
                pass
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/instances/{instance_id}/rebuild")
def rebuild_instance(
    instance_id: int,
    payload: InstanceRebuildRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Rebuild an instance after changing GPU count or memory, with backup protection."""
    if payload.num_gpus not in {0, 1, 2, 4, 8}:
        raise HTTPException(
            status_code=400,
            detail="Supported GPU counts are 0, 1, 2, 4, or 8.",
        )
    if payload.memory_gb % 8 != 0:
        raise HTTPException(
            status_code=400,
            detail="Memory must be allocated in 8 GB increments.",
        )

    instance = _get_instance_for_user(db, instance_id, current_user)
    instance_obj = cast(Any, instance)
    current_user_obj = cast(Any, current_user)
    current_gpu_count = len(list(instance_obj.gpu_indices))
    current_memory_gb = int(instance_obj.memory_gb)
    if payload.num_gpus == current_gpu_count and payload.memory_gb == current_memory_gb:
        raise HTTPException(status_code=400, detail="Configuration is unchanged.")

    db.refresh(current_user)
    usage = _get_running_usage(current_user)
    projected_gpu_usage = usage["used_gpu"] - current_gpu_count + payload.num_gpus
    if projected_gpu_usage > int(current_user_obj.quota_gpu):
        raise HTTPException(status_code=400, detail="GPU quota exceeded.")
    projected_memory_usage = (
        usage["used_memory_gb"] - current_memory_gb + payload.memory_gb
    )
    if projected_memory_usage > int(current_user_obj.quota_memory_gb):
        raise HTTPException(status_code=400, detail="Memory quota exceeded.")

    with gpu_manager.locked_allocation():
        try:
            selected_gpus = _choose_rebuild_gpu_indices(db, instance, payload.num_gpus)
            rebuilt_instance = _rebuild_instance_with_new_gpus(
                db,
                instance,
                current_user,
                selected_gpus,
                payload.memory_gb,
            )
            LOGGER.info(
                "Rebuilt instance %s for user %s with GPUs %s and memory %sGB",
                instance_obj.container_name,
                current_user_obj.username,
                selected_gpus,
                payload.memory_gb,
            )
            db.commit()
            db.refresh(rebuilt_instance)
            return _serialize_instance(rebuilt_instance)
        except HTTPException:
            db.rollback()
            raise
        except RuntimeError as exc:
            db.commit()
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/instances/{instance_id}/logs")
def get_instance_logs(
    instance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Fetch recent logs for one of the user's containers."""
    instance = _get_instance_for_user(db, instance_id, current_user)
    instance_obj = cast(Any, instance)
    try:
        return {"logs": container_manager.get_logs(str(instance_obj.container_name))}
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/gpus/status")
def gpu_status(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Return all GPU live status data."""
    del current_user
    return gpu_manager.get_gpu_status(db)


@app.get("/api/quota/me")
def my_quota(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Return current user's quota limits and usage."""
    user = (
        db.query(User)
        .options(joinedload(User.instances))
        .filter(User.id == current_user.id)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    user_obj = cast(Any, user)
    usage = _get_running_usage(user)
    return {
        **usage,
        "quota_gpu": user_obj.quota_gpu,
        "quota_memory_gb": user_obj.quota_memory_gb,
        "quota_max_instances": user_obj.quota_max_instances,
    }


@app.get("/api/admin/users")
def admin_list_users(
    admin_user: User = Depends(get_admin_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Return all users and their quota usage for administrators."""
    del admin_user
    users = (
        db.query(User)
        .options(joinedload(User.instances))
        .order_by(User.created_at.asc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for user in users:
        user_obj = cast(Any, user)
        usage = _get_running_usage(user)
        result.append(
            {
                "id": user_obj.id,
                "username": user_obj.username,
                "email": user_obj.email,
                "is_admin": user_obj.is_admin,
                "created_at": user_obj.created_at.isoformat(),
                **usage,
                "quota_gpu": user_obj.quota_gpu,
                "quota_memory_gb": user_obj.quota_memory_gb,
                "quota_max_instances": user_obj.quota_max_instances,
            }
        )
    return result


@app.put("/api/admin/users/{user_id}/quota")
def admin_update_quota(
    user_id: int,
    payload: QuotaUpdateRequest,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Update a user's resource quota."""
    del admin_user
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    user_obj = cast(Any, user)
    user_obj.quota_gpu = payload.quota_gpu
    user_obj.quota_memory_gb = payload.quota_memory_gb
    user_obj.quota_max_instances = payload.quota_max_instances
    db.commit()
    return {"message": "Quota updated."}


@app.get("/api/admin/instances")
def admin_list_instances(
    username: str | None = None,  # cluster_manager 适配：支持按用户名过滤实例
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return all managed instances for administrators."""
    del admin_user
    # cluster_manager 适配：有 username 参数时只返回该用户的实例
    query = db.query(Instance).options(joinedload(Instance.user))
    if username:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return []
        query = query.filter(Instance.user_id == user.id)
    instances = query.order_by(Instance.created_at.desc()).all()
    return [_serialize_instance(instance) for instance in instances]


@app.delete("/api/admin/instances/{instance_id}")
def admin_delete_instance(
    instance_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Force delete any instance as an administrator."""
    del admin_user
    instance = (
        db.query(Instance)
        .options(joinedload(Instance.user))
        .filter(Instance.id == instance_id)
        .first()
    )
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found.")
    _delete_instance(db, instance)
    return {"message": "Instance deleted."}


# ---------------------------------------------------------------------------
# FRP 相关 API — 供 VPS (Clustermanager) 使用
# ---------------------------------------------------------------------------


class FrpContainerInfo(BaseModel):
    """容器 FRP 连接信息."""

    container_name: str
    ssh_port: int
    secret_key: str


def verify_internal_service_token(
    x_internal_token: str | None = Header(default=None),
) -> None:
    """Validate service-to-service requests from Clustermanager."""
    expected_token = INTERNAL_SERVICE_TOKEN
    if x_internal_token is None or x_internal_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid internal service token")


@app.get("/api/frp/containers")
def list_frp_containers(
    _: None = Depends(verify_internal_service_token),
) -> list[FrpContainerInfo]:
    """返回所有容器的 FRP 连接信息（供 VPS visitor 使用）."""
    from frp_manager import FrpManager

    try:
        container_manager.sync_frp_config()
    except Exception as exc:
        LOGGER.warning("Failed to sync FRP config before listing containers: %s", exc)

    frp = FrpManager()
    containers = frp.get_ready_containers()

    result = []
    for c in containers:
        result.append(
            FrpContainerInfo(
                container_name=c["name"],
                ssh_port=c["ssh_port"],
                secret_key=frp.get_container_secret(c["name"]),
            )
        )
    return result


@app.get("/api/frp/containers/{container_name}")
def get_frp_container_info(
    container_name: str,
    _: None = Depends(verify_internal_service_token),
) -> FrpContainerInfo:
    """返回单个容器的 FRP 连接信息."""
    from frp_manager import FrpManager

    try:
        container_manager.sync_frp_config()
    except Exception as exc:
        LOGGER.warning("Failed to sync FRP config before querying container: %s", exc)

    frp = FrpManager()
    containers = frp.get_ready_containers()

    for c in containers:
        if c["name"] == container_name:
            return FrpContainerInfo(
                container_name=c["name"],
                ssh_port=c["ssh_port"],
                secret_key=frp.get_container_secret(c["name"]),
            )

    raise HTTPException(status_code=404, detail="Container not found in FRP config.")


@app.post("/api/frp/sync")
def sync_frp_config(
    _: None = Depends(verify_internal_service_token),
) -> dict[str, Any]:
    """手动触发 FRP 配置同步."""
    success = container_manager.sync_frp_config()
    return {"success": success, "message": "FRP config synced" if success else "Failed"}


class VpsAccessInfo(BaseModel):
    """VPS 访问信息模型."""

    vps_port: int
    vps_ip: str
    ssh_cmd: str


@app.post("/api/instances/{container_name}/vps-access")
def update_vps_access(
    container_name: str,
    vps_info: VpsAccessInfo,
    _: None = Depends(verify_internal_service_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新实例的 VPS 访问信息（由 ClusterManager 调用）."""

    instance = (
        db.query(Instance).filter(Instance.container_name == container_name).first()
    )

    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # 存储 VPS 访问信息
    instance_obj = cast(Any, instance)
    instance_obj.vps_access = {
        "vps_port": vps_info.vps_port,
        "vps_ip": vps_info.vps_ip,
        "ssh_cmd": vps_info.ssh_cmd,
    }
    db.commit()

    return {
        "success": True,
        "message": "VPS access info updated",
        "container_name": container_name,
        "vps_access": instance_obj.vps_access,
    }


@app.get("/api/instances/{container_name}/vps-access")
def get_vps_access(
    container_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取实例的 VPS 访问信息."""
    instance = (
        db.query(Instance).filter(Instance.container_name == container_name).first()
    )

    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # 检查权限（管理员或实例所有者）
    current_user_obj = cast(Any, current_user)
    instance_obj = cast(Any, instance)
    if not current_user_obj.is_admin and instance_obj.user_id != current_user_obj.id:
        raise HTTPException(status_code=403, detail="Permission denied")

    if not instance_obj.vps_access:
        return {"success": False, "message": "VPS access info not available"}

    return {"success": True, "vps_access": instance_obj.vps_access}
