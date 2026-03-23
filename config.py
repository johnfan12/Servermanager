"""Application configuration values.

配置优先级：环境变量 > .env 文件 > 默认值
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_int_csv(raw: str) -> list[int]:
    values: list[int] = []
    for item in _parse_csv(raw):
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values


# ============================================================================
# 基础服务配置
# ============================================================================

# 服务器 IP 地址（用于显示和连接）
SERVER_IP = os.environ.get("SERVER_IP", "[IP_ADDRESS]")

# 用户数据目录
DATA_DIR = os.environ.get("DATA_DIR", "/data/users")

# SSH 端口范围（容器映射）
_port_range_str = os.environ.get("PORT_RANGE", "20000-29999")
_port_range = _port_range_str.split("-")
PORT_RANGE = (
    (int(_port_range[0]), int(_port_range[1]))
    if "-" in _port_range_str and len(_port_range) == 2
    else (20000, 29999)
)

# 工作目录
LOG_DIR = BASE_DIR / os.environ.get("LOG_DIR", "logs")
LOCK_DIR = BASE_DIR / os.environ.get("LOCK_DIR", "runtime")
FALLBACK_DATA_DIR = str(BASE_DIR / "data" / "users")

# ============================================================================
# 安全认证配置
# ============================================================================

# JWT 密钥 — 必须与 cluster_manager 的 JWT_SECRET 保持一致！
# 这是 SSO 的唯一前提，否则跳转后 token 验证会返回 401
JWT_SECRET = os.environ.get("JWT_SECRET", "change-this-secret")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "24"))
ENV = os.environ.get("ENV", "dev").lower()

# 服务间鉴权密钥 — 用于 Clustermanager 调用内部 FRP/VPS 回写接口
INTERNAL_SERVICE_TOKEN = os.environ.get(
    "INTERNAL_SERVICE_TOKEN", "change-this-internal-service-token"
)

# 管理员账号
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# 是否允许注册
ALLOW_REGISTER = os.environ.get("ALLOW_REGISTER", "true").lower() == "true"

# ============================================================================
# 容器管理配置
# ============================================================================

# GPU 数量（用于生成完整 GPU 状态列表）
GPU_COUNT = int(os.environ.get("GPU_COUNT", "1"))

# 备份保留时间（分钟）
BACKUP_RETENTION_MINUTES = int(os.environ.get("BACKUP_RETENTION_MINUTES", "30"))

# 孤儿容器宽限期（分钟）
ORPHAN_CONTAINER_GRACE_MINUTES = int(
    os.environ.get("ORPHAN_CONTAINER_GRACE_MINUTES", "5")
)

# 默认进程限制
DEFAULT_PIDS_LIMIT = int(os.environ.get("DEFAULT_PIDS_LIMIT", "512"))

# 节点全局可分配内存上限（GB，按 running 实例统计）
NODE_ALLOCATABLE_MEMORY_GB = int(os.environ.get("NODE_ALLOCATABLE_MEMORY_GB", "256"))
if NODE_ALLOCATABLE_MEMORY_GB < 8:
    NODE_ALLOCATABLE_MEMORY_GB = 8

# 实例可选内存档位（GB，逗号分隔）
_memory_options = sorted(
    {
        value
        for value in _parse_int_csv(
            os.environ.get("INSTANCE_MEMORY_OPTIONS_GB", "8,16,32,64,128")
        )
        if value >= 8 and value % 8 == 0
    }
)
if not _memory_options:
    _memory_options = [8, 16, 32, 64, 128]

# 单实例内存上限（GB），用于限制可选档位和接口校验
MAX_INSTANCE_MEMORY_GB = int(
    os.environ.get("MAX_INSTANCE_MEMORY_GB", str(max(_memory_options)))
)
if MAX_INSTANCE_MEMORY_GB < 8:
    MAX_INSTANCE_MEMORY_GB = 8

INSTANCE_MEMORY_OPTIONS_GB: tuple[int, ...] = tuple(
    value for value in _memory_options if value <= MAX_INSTANCE_MEMORY_GB
)
if not INSTANCE_MEMORY_OPTIONS_GB:
    INSTANCE_MEMORY_OPTIONS_GB = (8,)

# ============================================================================
# 数据库配置
# ============================================================================

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://server_user:server_pass@127.0.0.1:5432/server_manager",
)

# ============================================================================
# Docker 镜像配置
# ============================================================================

AVAILABLE_IMAGES = {
    "pytorch": os.environ.get("IMAGE_PYTORCH", "lab/pytorch:2.3-cuda12.1"),
    "pytorch_old": os.environ.get("IMAGE_PYTORCH_OLD", "lab/pytorch:2.1-cuda11.8"),
    "tensorflow": os.environ.get("IMAGE_TENSORFLOW", "lab/tensorflow:2.15"),
    "base": os.environ.get("IMAGE_BASE", "lab/base:22.04"),
}

# ============================================================================
# FRP 配置 — 用于将容器 SSH 端口穿透到 VPS
# ============================================================================

FRP_ENABLED = os.environ.get("FRP_ENABLED", "true").lower() == "true"
FRP_SERVER_ADDR = os.environ.get("FRP_SERVER_ADDR", "your-vps-public-ip")
FRP_SERVER_PORT = int(os.environ.get("FRP_SERVER_PORT", "7000"))
FRP_TOKEN = os.environ.get("FRP_TOKEN", "your-frp-secret-token")
FRP_CONFIG_DIR = Path(os.environ.get("FRP_CONFIG_DIR", "/etc/frp"))
FRP_CONFIG_FILE = FRP_CONFIG_DIR / "frpc-containers.ini"
FRP_CONTAINER_CONFIG_DIR = Path(
    os.environ.get("FRP_CONTAINER_CONFIG_DIR", str(FRP_CONFIG_DIR / "containers"))
)
FRP_CONTAINER_SK_PREFIX = os.environ.get("FRP_CONTAINER_SK_PREFIX", "gpu-container")

# CORS 配置（默认仅本地调试）
CORS_ALLOW_ORIGINS: tuple[str, ...] = tuple(
    _parse_csv(os.environ.get("CORS_ALLOW_ORIGINS", "http://localhost:9999"))
)
CORS_ALLOW_CREDENTIALS: bool = (
    os.environ.get("CORS_ALLOW_CREDENTIALS", "true").lower() == "true"
)


def _ensure_secure_production_config() -> None:
    if ENV != "prod":
        return

    invalid_values = {
        "JWT_SECRET": {"", "change-this-secret", "change-this-to-a-strong-secret-key"},
        "INTERNAL_SERVICE_TOKEN": {
            "",
            "change-this-internal-service-token",
        },
        "ADMIN_PASSWORD": {"", "admin123", "your-strong-admin-password"},
        "FRP_TOKEN": {"", "your-frp-secret-token"},
    }

    bad = [
        key
        for key, disallowed in invalid_values.items()
        if str(globals().get(key, "")) in disallowed
    ]
    if bad:
        raise RuntimeError(
            "Refusing to start in ENV=prod with insecure config: " + ", ".join(bad)
        )


_ensure_secure_production_config()
