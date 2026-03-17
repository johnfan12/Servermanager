"""Application configuration values.

配置优先级：环境变量 > .env 文件 > 默认值
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

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

# ============================================================================
# 数据库配置
# ============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./servermanager.db")

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
FRP_CONTAINER_SK_PREFIX = os.environ.get("FRP_CONTAINER_SK_PREFIX", "gpu-container")
