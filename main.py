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
    CORS_ALLOW_CREDENTIALS,
    CORS_ALLOW_ORIGINS,
    ENV,
    INSTANCE_MEMORY_OPTIONS_GB,
    INTERNAL_SERVICE_TOKEN,
    JWT_SECRET,
    LOG_DIR,
    MAX_INSTANCE_MEMORY_GB,
    NODE_ALLOCATABLE_MEMORY_GB,
    SERVER_IP,
)
from container_manager import ContainerManager
from database import SessionLocal, get_db, init_db
from gpu_manager import GPUManager
from models import GPUAllocation, Instance, User, UserSSHKey
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
DISPLAY_NAME_MAX_LENGTH = 64


def _unique_name(prefix: str) -> str:
    """Return a collision-resistant resource name."""
    return f"{prefix}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{secrets.token_hex(3)}"


def _normalize_display_name(value: str | None) -> str | None:
    """Normalize an optional user-facing instance name."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > DISPLAY_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Instance name cannot exceed {DISPLAY_NAME_MAX_LENGTH} characters.",
        )
    if any(ord(char) < 32 for char in normalized):
        raise HTTPException(status_code=400, detail="Instance name contains control characters.")
    return normalized


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
        container_manager.frp_manager.sync_api_client_config()
    except Exception as exc:
        LOGGER.warning("Failed to sync FRP API config on startup: %s", exc)

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


class InternalSSHKeySyncRequest(BaseModel):
    """One SSH public key row pushed from cluster manager."""

    public_key: str = Field(min_length=1, max_length=8192)
    remark: str = Field(default="", max_length=255)
    fingerprint: str = Field(min_length=1, max_length=255)


class InternalUserSyncRequest(BaseModel):
    """Request body for Clustermanager -> node user sync."""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    email: str = Field(min_length=3, max_length=255)
    password_hash: str = Field(min_length=1, max_length=255)
    is_admin: bool = False
    ssh_public_keys: list[InternalSSHKeySyncRequest] = Field(default_factory=list)


class InstanceCreateRequest(BaseModel):
    """Request body for creating a new instance."""

    num_gpus: int = Field(ge=0)
    memory_gb: int = Field(ge=8)
    image: str
    expire_hours: int = Field(ge=24, le=168)
    display_name: str | None = Field(default=None, max_length=128)


class InstanceRenewRequest(BaseModel):
    """Request body for extending an instance expiration time."""

    extend_days: int = Field(ge=1, le=7)


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
        "display_name": instance_obj.display_name,
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


def _get_node_running_memory_gb(db: Session) -> int:
    """Compute total memory usage of all running instances on this node."""
    running_instances = db.query(Instance).filter(Instance.status == "running").all()
    total = 0
    for instance in running_instances:
        total += int(cast(Any, instance).memory_gb)
    return total


def _get_user_authorized_keys(db: Session, user_id: int) -> list[str]:
    """Return all synced SSH public keys for one user."""
    keys = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.user_id == user_id)
        .order_by(UserSSHKey.created_at.asc(), UserSSHKey.id.asc())
        .all()
    )
    return [str(key.public_key) for key in keys if str(key.public_key).strip()]


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
    cleanup_workspace = container_manager.locate_instance_workspace_cleanup_dir(
        str(instance_obj.user.username), str(instance_obj.container_name)
    )
    try:
        container_manager.remove_container(str(instance_obj.container_name))
    except RuntimeError as exc:
        if "not found" not in str(exc).lower():
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    if cleanup_workspace is not None:
        try:
            container_manager.remove_workspace(cleanup_workspace)
        except RuntimeError as exc:
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
        LOGGER.info(
            "No running event loop for delayed cleanup; removing backup now: %s",
            backup_path,
        )
        try:
            container_manager.remove_workspace(backup_path)
        except RuntimeError as exc:
            LOGGER.warning("Failed to remove temporary backup %s: %s", backup_path, exc)


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
            authorized_keys=_get_user_authorized_keys(db, int(user_obj.id)),
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
def get_meta(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return frontend bootstrap metadata."""
    node_memory_used_gb = _get_node_running_memory_gb(db)
    node_memory_free_gb = max(0, NODE_ALLOCATABLE_MEMORY_GB - node_memory_used_gb)
    return {
        "server_ip": SERVER_IP,
        "allow_register": ALLOW_REGISTER,
        "memory_options_gb": list(INSTANCE_MEMORY_OPTIONS_GB),
        "max_instance_memory_gb": MAX_INSTANCE_MEMORY_GB,
        "node_allocatable_memory_gb": NODE_ALLOCATABLE_MEMORY_GB,
        "node_memory_used_gb": node_memory_used_gb,
        "node_memory_free_gb": node_memory_free_gb,
    }


@app.get("/api/images")
def get_images() -> dict[str, Any]:
    """Return image options for create/rebuild UI.

    key: 前端提交到创建接口的镜像键
    label: 展示名称
    image_ref: 节点实际 Docker 镜像引用
    """
    try:
        local_images = container_manager.list_local_images()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    images = [
        {
            "key": image["image_ref"],
            "label": image["image_ref"],
            "image_ref": image["image_ref"],
        }
        for image in local_images
    ]
    return {"images": images}


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
    if payload.memory_gb > MAX_INSTANCE_MEMORY_GB:
        raise HTTPException(
            status_code=400,
            detail=f"Memory cannot exceed {MAX_INSTANCE_MEMORY_GB} GB.",
        )
    if payload.memory_gb not in INSTANCE_MEMORY_OPTIONS_GB:
        allowed = ", ".join(str(value) for value in INSTANCE_MEMORY_OPTIONS_GB)
        raise HTTPException(
            status_code=400,
            detail=f"Memory must be one of configured options: {allowed} GB.",
        )
    try:
        resolved_image_ref = container_manager.ensure_image_available(payload.image)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.expire_hours % 24 != 0:
        raise HTTPException(
            status_code=400, detail="Expiration must be in full-day increments."
        )
    normalized_display_name = _normalize_display_name(payload.display_name)

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

    node_running_memory_gb = _get_node_running_memory_gb(db)
    projected_node_memory_gb = node_running_memory_gb + payload.memory_gb
    if projected_node_memory_gb > NODE_ALLOCATABLE_MEMORY_GB:
        raise HTTPException(
            status_code=400,
            detail=(
                "Node allocatable memory exceeded. "
                f"Used {node_running_memory_gb} GB, requested +{payload.memory_gb} GB, "
                f"limit {NODE_ALLOCATABLE_MEMORY_GB} GB."
            ),
        )

    cpu_cores = max(4, payload.num_gpus * 8)
    expire_at = datetime.utcnow() + timedelta(hours=payload.expire_hours)
    container_name = _unique_name(f"gpu_user_{current_user_obj.username}")
    display_name = normalized_display_name or container_name
    existing_instance = (
        db.query(Instance)
        .filter(
            Instance.user_id == current_user.id,
            Instance.display_name == display_name,
        )
        .first()
    )
    if existing_instance is not None:
        raise HTTPException(status_code=400, detail="Instance name is already in use.")
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
                display_name=display_name,
                gpu_indices=selected_gpus,
                memory_gb=payload.memory_gb,
                cpu_cores=cpu_cores,
                image_name=resolved_image_ref,
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
                image_name=resolved_image_ref,
                container_name=container_name,
                workspace_dir=workspace_path,
                authorized_keys=_get_user_authorized_keys(db, int(current_user_obj.id)),
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
    expire_at = instance_obj.expire_at
    if expire_at is not None and expire_at <= datetime.utcnow():
        raise HTTPException(
            status_code=400,
            detail="Instance has expired. Please renew before restarting.",
        )

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


@app.post("/api/instances/{instance_id}/renew")
def renew_instance(
    instance_id: int,
    payload: InstanceRenewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Extend instance expiration time when remaining time is under 7 days."""
    instance = _get_instance_for_user(db, instance_id, current_user)
    instance_obj = cast(Any, instance)
    now = datetime.utcnow()
    expire_at = instance_obj.expire_at
    if expire_at is None:
        raise HTTPException(
            status_code=400, detail="Instance does not support renewal."
        )

    remaining_seconds = (expire_at - now).total_seconds()
    if remaining_seconds > 7 * 24 * 3600:
        raise HTTPException(
            status_code=400,
            detail="Renewal is only allowed when less than 7 days remain.",
        )

    base_time = expire_at if expire_at > now else now
    instance_obj.expire_at = base_time + timedelta(days=payload.extend_days)
    db.commit()
    db.refresh(instance)
    return {
        "message": "Instance renewed.",
        "instance": _serialize_instance(instance),
    }


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
    if payload.memory_gb > MAX_INSTANCE_MEMORY_GB:
        raise HTTPException(
            status_code=400,
            detail=f"Memory cannot exceed {MAX_INSTANCE_MEMORY_GB} GB.",
        )
    if payload.memory_gb not in INSTANCE_MEMORY_OPTIONS_GB:
        allowed = ", ".join(str(value) for value in INSTANCE_MEMORY_OPTIONS_GB)
        raise HTTPException(
            status_code=400,
            detail=f"Memory must be one of configured options: {allowed} GB.",
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

    node_running_memory_gb = _get_node_running_memory_gb(db)
    projected_node_memory_gb = node_running_memory_gb - current_memory_gb + payload.memory_gb
    if projected_node_memory_gb > NODE_ALLOCATABLE_MEMORY_GB:
        raise HTTPException(
            status_code=400,
            detail=(
                "Node allocatable memory exceeded after rebuild. "
                f"Used {node_running_memory_gb} GB, projected {projected_node_memory_gb} GB, "
                f"limit {NODE_ALLOCATABLE_MEMORY_GB} GB."
            ),
        )

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


@app.post("/api/internal/users/sync")
def sync_user_from_cluster(
    payload: InternalUserSyncRequest,
    _: None = Depends(verify_internal_service_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Idempotently create or refresh one node-local user from central auth data."""
    user = db.query(User).filter(User.username == payload.username).first()
    created = False

    if user is None:
        user = User(
            username=payload.username,
            email=payload.email,
            password_hash=payload.password_hash,
            is_admin=payload.is_admin,
        )
        db.add(user)
        created = True
    else:
        user_obj = cast(Any, user)
        user_obj.password_hash = payload.password_hash
        user_obj.is_admin = payload.is_admin

        existing_email_owner = (
            db.query(User)
            .filter(User.email == payload.email, User.username != payload.username)
            .first()
        )
        if existing_email_owner is None:
            user_obj.email = payload.email
        else:
            LOGGER.warning(
                "Skipped email sync for user=%s because email=%s is already used by user=%s",
                payload.username,
                payload.email,
                cast(Any, existing_email_owner).username,
            )

    db.flush()
    user_obj = cast(Any, user)
    existing_keys = (
        db.query(UserSSHKey)
        .filter(UserSSHKey.user_id == int(user_obj.id))
        .all()
    )
    existing_by_fingerprint = {
        str(key.fingerprint): key
        for key in existing_keys
        if str(key.fingerprint or "").strip()
    }
    incoming_fingerprints: set[str] = set()
    for key_payload in payload.ssh_public_keys:
        fingerprint = str(key_payload.fingerprint)
        incoming_fingerprints.add(fingerprint)
        existing_key = existing_by_fingerprint.get(fingerprint)
        if existing_key is None:
            db.add(
                UserSSHKey(
                    user_id=int(user_obj.id),
                    public_key=key_payload.public_key,
                    remark=key_payload.remark,
                    fingerprint=fingerprint,
                )
            )
        else:
            existing_key.public_key = key_payload.public_key
            existing_key.remark = key_payload.remark

    for fingerprint, existing_key in existing_by_fingerprint.items():
        if fingerprint not in incoming_fingerprints:
            db.delete(existing_key)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Failed to sync user to node.") from exc

    LOGGER.info(
        "Synced cluster user to node user=%s created=%s is_admin=%s ssh_keys=%s",
        payload.username,
        created,
        payload.is_admin,
        len(payload.ssh_public_keys),
    )
    return {
        "success": True,
        "created": created,
        "username": payload.username,
        "ssh_key_count": len(payload.ssh_public_keys),
    }


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
