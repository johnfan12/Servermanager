"""Simplified node backend for exposing local services through FRP tunnels."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from auth import (
    Principal,
    authenticate_local_user,
    create_access_token,
    get_current_principal,
    is_admin_username,
    is_local_user_allowed,
    verify_internal_token,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

LOG_DIR = BASE_DIR / os.environ.get("SIMPLE_LOG_DIR", "logs")
RUNTIME_DIR = BASE_DIR / os.environ.get("SIMPLE_RUNTIME_DIR", "runtime")
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "simple-servermanager.log"),
        logging.StreamHandler(),
    ],
)
LOGGER = logging.getLogger("simple_servermanager")

DATABASE_URL = os.environ.get(
    "SIMPLE_DATABASE_URL",
    f"sqlite:///{BASE_DIR / 'simple_servermanager.db'}",
)
connect_args: dict[str, Any] = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def _normalize_public_host(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return "127.0.0.1"
    if "://" in candidate:
        parsed = urlparse(candidate)
        return parsed.hostname or parsed.netloc or candidate
    candidate = candidate.split("/", 1)[0]
    if candidate.count(":") == 1:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            return host
    return candidate


INTERNAL_SERVICE_TOKEN = os.environ.get("SIMPLE_INTERNAL_SERVICE_TOKEN") or os.environ.get(
    "INTERNAL_SERVICE_TOKEN", "change-this-internal-service-token"
)
PUBLIC_HOST = _normalize_public_host(
    os.environ.get("SIMPLE_PUBLIC_HOST")
    or os.environ.get("SERVER_IP", os.environ.get("FRP_SERVER_ADDR", "127.0.0.1"))
)
FRP_ENABLED = os.environ.get("SIMPLE_FRP_ENABLED", os.environ.get("FRP_ENABLED", "true")).lower() == "true"
FRP_SERVER_ADDR = os.environ.get("SIMPLE_FRP_SERVER_ADDR") or os.environ.get(
    "FRP_SERVER_ADDR", "127.0.0.1"
)
FRP_SERVER_PORT = int(os.environ.get("SIMPLE_FRP_SERVER_PORT", os.environ.get("FRP_SERVER_PORT", "7000")))
FRP_TOKEN = os.environ.get("SIMPLE_FRP_TOKEN") or os.environ.get("FRP_TOKEN", "")
FRP_CLIENT_BIN = os.environ.get("SIMPLE_FRP_CLIENT_BIN") or os.environ.get("FRP_CLIENT_BIN", "frpc")
FRPC_CONFIG_FILE = Path(
    os.environ.get("SIMPLE_FRPC_CONFIG_FILE", str(RUNTIME_DIR / "simple-frpc-tunnels.ini"))
)
FRPC_PID_FILE = Path(
    os.environ.get("SIMPLE_FRPC_PID_FILE", str(RUNTIME_DIR / "simple-frpc-tunnels.pid"))
)
FRPC_LOG_FILE = Path(
    os.environ.get("SIMPLE_FRPC_LOG_FILE", str(LOG_DIR / "simple-frpc-tunnels.log"))
)
REMOTE_PORT_RANGE_RAW = os.environ.get("SIMPLE_TUNNEL_REMOTE_PORT_RANGE", "30000-39999")
SSH_LOCAL_HOST = os.environ.get("SIMPLE_SSH_LOCAL_HOST", "127.0.0.1")
SSH_LOCAL_PORT = int(os.environ.get("SIMPLE_SSH_LOCAL_PORT", "22"))
SSH_TUNNEL_ENABLED = os.environ.get("SIMPLE_SSH_TUNNEL_ENABLED", "true").lower() == "true"
SSH_PROXY_NAME = os.environ.get("SIMPLE_SSH_PROXY_NAME", "simple-node-ssh")
ALLOW_CUSTOM_TUNNELS = os.environ.get("SIMPLE_ALLOW_CUSTOM_TUNNELS", "false").lower() == "true"
ALLOW_NON_LOOPBACK = os.environ.get("SIMPLE_ALLOW_NON_LOOPBACK", "false").lower() == "true"
REQUIRE_LISTENING_LOCAL_PORT = (
    os.environ.get("SIMPLE_REQUIRE_LISTENING_LOCAL_PORT", "false").lower() == "true"
)
STOP_TUNNELS_ON_EXIT = os.environ.get("SIMPLE_STOP_TUNNELS_ON_EXIT", "true").lower() == "true"
CORS_ALLOW_ORIGINS = [
    item.strip()
    for item in os.environ.get("SIMPLE_CORS_ALLOW_ORIGINS", "http://localhost:9999").split(",")
    if item.strip()
]


class SimpleTunnel(Base):
    """A user-owned FRP TCP tunnel."""

    __tablename__ = "simple_tunnels"

    id = Column(Integer, primary_key=True, index=True)
    owner = Column(String(64), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    protocol = Column(String(16), nullable=False, default="tcp")
    local_host = Column(String(255), nullable=False, default="127.0.0.1")
    local_port = Column(Integer, nullable=False)
    remote_port = Column(Integer, nullable=False, unique=True, index=True)
    status = Column(String(32), nullable=False, default="configured")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class LoginRequest(BaseModel):
    """Login payload for local PAM authentication."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=4096)


class TunnelCreateRequest(BaseModel):
    """Create one SSH tunnel to the local server by default."""

    name: str | None = Field(default=None, max_length=64)
    local_host: str = Field(default=SSH_LOCAL_HOST, min_length=1, max_length=255)
    local_port: int = Field(default=SSH_LOCAL_PORT, ge=1, le=65535)
    remote_port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("Tunnel name contains control characters.")
        return normalized

    @field_validator("local_host")
    @classmethod
    def normalize_local_host(cls, value: str) -> str:
        return value.strip()


def _parse_port_range(raw: str) -> tuple[int, int]:
    parts = raw.split("-", maxsplit=1)
    if len(parts) != 2:
        return (30000, 39999)
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        return (30000, 39999)
    if start < 1 or end > 65535 or start > end:
        return (30000, 39999)
    return (start, end)


REMOTE_PORT_RANGE = _parse_port_range(REMOTE_PORT_RANGE_RAW)
SSH_REMOTE_PORT = int(os.environ.get("SIMPLE_SSH_REMOTE_PORT", str(REMOTE_PORT_RANGE[0])))


def init_db() -> None:
    """Create the small standalone schema used by the simplified backend."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Any:
    """Provide a database session for FastAPI endpoints."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_internal_principal(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_user: str | None = Header(default=None, alias="X-User"),
    x_user_is_admin: str = Header(default="false", alias="X-User-Is-Admin"),
) -> Principal:
    verify_internal_token(x_internal_token, INTERNAL_SERVICE_TOKEN)
    username = str(x_user or "").strip()
    if not username or not is_local_user_allowed(username):
        raise HTTPException(status_code=403, detail="User is not a local account on this node.")
    is_admin = x_user_is_admin.lower() in {"1", "true", "yes", "on"} or is_admin_username(username)
    return Principal(username=username, is_admin=is_admin)


def _ensure_local_host_allowed(local_host: str) -> None:
    if ALLOW_NON_LOOPBACK:
        return
    normalized = local_host.lower()
    if normalized == "localhost":
        return
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return
    except ValueError:
        pass
    raise HTTPException(
        status_code=400,
        detail="Only loopback local hosts are allowed by default. Set SIMPLE_ALLOW_NON_LOOPBACK=true to change this.",
    )


def _is_port_listening(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.8)
        return sock.connect_ex((host, port)) == 0


def _validate_tunnel_request(payload: TunnelCreateRequest) -> None:
    _ensure_local_host_allowed(payload.local_host)
    if not ALLOW_CUSTOM_TUNNELS and (
        payload.local_host != SSH_LOCAL_HOST or payload.local_port != SSH_LOCAL_PORT
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only SSH access is allowed by default. "
                "Set SIMPLE_ALLOW_CUSTOM_TUNNELS=true to expose other local ports."
            ),
        )
    if REQUIRE_LISTENING_LOCAL_PORT and not _is_port_listening(payload.local_host, payload.local_port):
        raise HTTPException(status_code=400, detail="The local port is not listening.")


def _allocate_remote_port(db: Session, preferred_port: int | None = None) -> int:
    start, end = REMOTE_PORT_RANGE
    used_ports = set(db.execute(select(SimpleTunnel.remote_port)).scalars().all())
    if preferred_port is not None:
        if preferred_port < start or preferred_port > end:
            raise HTTPException(
                status_code=400,
                detail=f"Remote port must be inside {start}-{end}.",
            )
        if preferred_port in used_ports:
            raise HTTPException(status_code=409, detail="Remote port is already allocated.")
        return preferred_port

    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    raise HTTPException(status_code=409, detail="No remote ports are available.")


def _safe_section_name(tunnel: SimpleTunnel) -> str:
    raw = f"simple-{tunnel.id}-{tunnel.owner}-{tunnel.remote_port}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")


def _render_frpc_config(tunnels: list[SimpleTunnel]) -> str:
    lines = [
        "[common]",
        f"server_addr = {FRP_SERVER_ADDR}",
        f"server_port = {FRP_SERVER_PORT}",
        f"token = {FRP_TOKEN}",
        "",
    ]
    for tunnel in tunnels:
        lines.extend(
            [
                f"[{_safe_section_name(tunnel)}]",
                "type = tcp",
                f"local_ip = {tunnel.local_host}",
                f"local_port = {tunnel.local_port}",
                f"remote_port = {tunnel.remote_port}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _render_fixed_ssh_frpc_config() -> str:
    lines = [
        "[common]",
        f"server_addr = {FRP_SERVER_ADDR}",
        f"server_port = {FRP_SERVER_PORT}",
        f"token = {FRP_TOKEN}",
        "",
        f"[{SSH_PROXY_NAME}]",
        "type = tcp",
        f"local_ip = {SSH_LOCAL_HOST}",
        f"local_port = {SSH_LOCAL_PORT}",
        f"remote_port = {SSH_REMOTE_PORT}",
        "",
    ]
    return "\n".join(lines).strip() + "\n"


def _read_pid() -> int | None:
    try:
        return int(FRPC_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _frpc_config_pids() -> set[int]:
    config_path = str(FRPC_CONFIG_FILE)
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()

    pids: set[int] = set()
    current_pid = os.getpid()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, args = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if "frpc" in args and config_path in args:
            pids.add(pid)
    return pids


def _stop_frpc_process() -> None:
    pids: set[int] = set()
    pid = _read_pid()
    if pid is not None:
        pids.add(pid)
    pids.update(_frpc_config_pids())

    live_pids = {pid for pid in pids if _process_exists(pid)}
    if not live_pids:
        FRPC_PID_FILE.unlink(missing_ok=True)
        return

    for pid in live_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        live_pids = {pid for pid in live_pids if _process_exists(pid)}
        if not live_pids:
            break
        time.sleep(0.1)
    for pid in live_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    FRPC_PID_FILE.unlink(missing_ok=True)


def _resolve_frpc_binary() -> str | None:
    if Path(FRP_CLIENT_BIN).is_absolute():
        return FRP_CLIENT_BIN if Path(FRP_CLIENT_BIN).exists() else None
    return shutil.which(FRP_CLIENT_BIN)


def _tail_log(path: Path, limit: int = 4000) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode(errors="replace").strip()
    except OSError:
        return ""


def sync_frpc_config(db: Session) -> tuple[str, str | None]:
    """Render and restart the standalone FRP client process for all tunnels."""
    tunnels = list(db.execute(select(SimpleTunnel).order_by(SimpleTunnel.id)).scalars().all())
    FRPC_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FRPC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FRPC_CONFIG_FILE.write_text(_render_frpc_config(tunnels), encoding="utf-8")

    if not tunnels:
        _stop_frpc_process()
        return ("configured", None)

    if not FRP_ENABLED:
        message = "FRP is disabled; tunnel config was rendered only."
        _set_tunnel_status(db, tunnels, "configured", message)
        return ("configured", message)
    if not FRP_TOKEN:
        message = "FRP token is empty."
        _set_tunnel_status(db, tunnels, "error", message)
        return ("error", message)

    frpc_binary = _resolve_frpc_binary()
    if frpc_binary is None:
        message = f"frpc binary was not found: {FRP_CLIENT_BIN}"
        _set_tunnel_status(db, tunnels, "error", message)
        return ("error", message)

    _stop_frpc_process()
    log_fh = FRPC_LOG_FILE.open("ab")
    try:
        process = subprocess.Popen(
            [frpc_binary, "-c", str(FRPC_CONFIG_FILE)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_fh.close()

    FRPC_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    time.sleep(0.35)
    if process.poll() is not None:
        message = _tail_log(FRPC_LOG_FILE) or "frpc exited immediately."
        _set_tunnel_status(db, tunnels, "error", message)
        return ("error", message)

    _set_tunnel_status(db, tunnels, "active", None)
    return ("active", None)


def sync_fixed_ssh_frpc_config() -> tuple[str, str | None]:
    """Render and restart the fixed per-node SSH FRP client."""
    FRPC_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FRPC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not SSH_TUNNEL_ENABLED:
        FRPC_CONFIG_FILE.write_text(
            _render_frpc_config([]),
            encoding="utf-8",
        )
        _stop_frpc_process()
        return ("disabled", None)

    FRPC_CONFIG_FILE.write_text(_render_fixed_ssh_frpc_config(), encoding="utf-8")

    if not FRP_ENABLED:
        return ("configured", "FRP is disabled; fixed SSH config was rendered only.")
    if not FRP_TOKEN:
        return ("error", "FRP token is empty.")

    frpc_binary = _resolve_frpc_binary()
    if frpc_binary is None:
        return ("error", f"frpc binary was not found: {FRP_CLIENT_BIN}")

    _stop_frpc_process()
    log_fh = FRPC_LOG_FILE.open("ab")
    try:
        process = subprocess.Popen(
            [frpc_binary, "-c", str(FRPC_CONFIG_FILE)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_fh.close()

    FRPC_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    time.sleep(0.35)
    if process.poll() is not None:
        return ("error", _tail_log(FRPC_LOG_FILE) or "frpc exited immediately.")
    return ("active", None)


def _set_tunnel_status(
    db: Session,
    tunnels: list[SimpleTunnel],
    status: str,
    error: str | None,
) -> None:
    now = datetime.utcnow()
    for tunnel in tunnels:
        tunnel.status = status
        tunnel.error = error
        tunnel.updated_at = now
    db.commit()


def _serialize_tunnel(tunnel: SimpleTunnel) -> dict[str, Any]:
    address = f"{PUBLIC_HOST}:{tunnel.remote_port}"
    ssh_command = f"ssh -p {tunnel.remote_port} {tunnel.owner}@{PUBLIC_HOST}"
    return {
        "id": int(tunnel.id),
        "owner": str(tunnel.owner),
        "name": str(tunnel.name),
        "protocol": str(tunnel.protocol),
        "local_host": str(tunnel.local_host),
        "local_port": int(tunnel.local_port),
        "public_host": PUBLIC_HOST,
        "remote_port": int(tunnel.remote_port),
        "address": address,
        "url": address,
        "ssh_command": ssh_command,
        "target": "ssh" if tunnel.local_port == SSH_LOCAL_PORT else "tcp",
        "status": str(tunnel.status),
        "error": tunnel.error,
        "created_at": tunnel.created_at.isoformat(),
        "updated_at": tunnel.updated_at.isoformat(),
    }


def _serialize_fixed_ssh_access(principal: Principal, status: str = "active", error: str | None = None) -> dict[str, Any]:
    address = f"{PUBLIC_HOST}:{SSH_REMOTE_PORT}"
    ssh_command = f"ssh -p {SSH_REMOTE_PORT} {principal.username}@{PUBLIC_HOST}"
    return {
        "id": f"fixed-{SSH_REMOTE_PORT}",
        "owner": principal.username,
        "name": "ssh",
        "protocol": "tcp",
        "target": "ssh",
        "local_host": SSH_LOCAL_HOST,
        "local_port": SSH_LOCAL_PORT,
        "public_host": PUBLIC_HOST,
        "remote_port": SSH_REMOTE_PORT,
        "address": address,
        "url": address,
        "ssh_command": ssh_command,
        "status": status,
        "error": error,
        "fixed": True,
    }


def _visible_tunnels(db: Session, principal: Principal, include_all: bool = False) -> list[SimpleTunnel]:
    statement = select(SimpleTunnel).order_by(SimpleTunnel.created_at.desc(), SimpleTunnel.id.desc())
    if not (principal.is_admin and include_all):
        statement = statement.where(SimpleTunnel.owner == principal.username)
    return list(db.execute(statement).scalars().all())


def _create_tunnel(
    db: Session,
    principal: Principal,
    payload: TunnelCreateRequest,
) -> SimpleTunnel:
    _validate_tunnel_request(payload)
    remote_port = _allocate_remote_port(db, payload.remote_port)
    name = payload.name or "ssh"
    tunnel = SimpleTunnel(
        owner=principal.username,
        name=name,
        protocol="tcp",
        local_host=payload.local_host,
        local_port=payload.local_port,
        remote_port=remote_port,
        status="configured",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(tunnel)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Remote port is already allocated.") from exc

    sync_frpc_config(db)
    db.refresh(tunnel)
    LOGGER.info(
        "Created tunnel owner=%s local=%s:%s remote=%s",
        principal.username,
        payload.local_host,
        payload.local_port,
        remote_port,
    )
    return tunnel


def _delete_tunnel(db: Session, principal: Principal, tunnel_id: int) -> None:
    tunnel = db.get(SimpleTunnel, tunnel_id)
    if tunnel is None:
        raise HTTPException(status_code=404, detail="Tunnel not found.")
    if tunnel.owner != principal.username and not principal.is_admin:
        raise HTTPException(status_code=403, detail="Tunnel belongs to another user.")

    db.delete(tunnel)
    db.commit()
    sync_frpc_config(db)
    LOGGER.info("Deleted tunnel id=%s owner=%s by=%s", tunnel_id, tunnel.owner, principal.username)


@asynccontextmanager
async def lifespan(application: FastAPI):
    del application
    init_db()
    try:
        status, error = sync_fixed_ssh_frpc_config()
        if error:
            LOGGER.warning("Fixed SSH FRP sync ended with status=%s error=%s", status, error)
    except Exception as exc:
        LOGGER.warning("Failed to sync fixed SSH FRP config on startup: %s", exc)
    try:
        yield
    finally:
        if STOP_TUNNELS_ON_EXIT:
            _stop_frpc_process()


app = FastAPI(title="Simple Server Manager", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "Simple Servermanager",
        "mode": "tunnel-only",
        "docs": "/docs",
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    fixed_status = "active" if _read_pid() is not None or _frpc_config_pids() else "stopped"
    return {
        "ok": True,
        "mode": "fixed-ssh",
        "frp_enabled": FRP_ENABLED,
        "public_host": PUBLIC_HOST,
        "ssh": {
            "enabled": SSH_TUNNEL_ENABLED,
            "status": fixed_status,
            "local_host": SSH_LOCAL_HOST,
            "local_port": SSH_LOCAL_PORT,
            "remote_port": SSH_REMOTE_PORT,
        },
        "remote_port_range": {
            "start": REMOTE_PORT_RANGE[0],
            "end": REMOTE_PORT_RANGE[1],
        },
    }


@app.post("/api/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    principal = authenticate_local_user(payload.username, payload.password)
    return {
        "access_token": create_access_token(principal),
        "token_type": "bearer",
        "user": {
            "username": principal.username,
            "is_admin": principal.is_admin,
        },
    }


@app.get("/api/auth/me")
def me(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"username": principal.username, "is_admin": principal.is_admin}


@app.get("/api/tunnels")
def list_tunnels(
    include_all: bool = Query(default=False),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    del include_all, db
    return {"tunnels": [_serialize_fixed_ssh_access(principal)]}


@app.post("/api/tunnels")
def create_tunnel(
    payload: TunnelCreateRequest,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    del payload, db
    return {"tunnel": _serialize_fixed_ssh_access(principal)}


@app.delete("/api/tunnels/{tunnel_id}")
def delete_tunnel(
    tunnel_id: int,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    del tunnel_id, principal, db
    return {"deleted": True}


@app.get("/api/internal/users/{username}")
def internal_user_exists(
    username: str,
    _: Principal = Depends(_require_internal_principal),
) -> dict[str, Any]:
    allowed = is_local_user_allowed(username)
    return {
        "username": username,
        "exists": allowed,
        "is_admin": is_admin_username(username) if allowed else False,
    }


@app.get("/api/internal/tunnels")
def internal_list_tunnels(
    include_all: bool = Query(default=False),
    principal: Principal = Depends(_require_internal_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    del include_all, db
    return {"tunnels": [_serialize_fixed_ssh_access(principal)]}


@app.post("/api/internal/tunnels")
def internal_create_tunnel(
    payload: TunnelCreateRequest,
    principal: Principal = Depends(_require_internal_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    del payload, db
    return {"tunnel": _serialize_fixed_ssh_access(principal)}


@app.delete("/api/internal/tunnels/{tunnel_id}")
def internal_delete_tunnel(
    tunnel_id: int,
    principal: Principal = Depends(_require_internal_principal),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    del tunnel_id, principal, db
    return {"deleted": True}


@app.get("/api/ssh-access")
def ssh_access(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"access": _serialize_fixed_ssh_access(principal)}


@app.get("/api/internal/ssh-access")
def internal_ssh_access(principal: Principal = Depends(_require_internal_principal)) -> dict[str, Any]:
    return {"access": _serialize_fixed_ssh_access(principal)}
