"""Application configuration values."""

import os
from pathlib import Path

SERVER_IP = "[IP_ADDRESS]"
DATA_DIR = "/data/users"
PORT_RANGE = (20000, 29999)
JWT_SECRET = os.environ.get("JWT_SECRET", "change-this-secret")
JWT_EXPIRE_HOURS = 24
BACKUP_RETENTION_MINUTES = 30
ORPHAN_CONTAINER_GRACE_MINUTES = 5
DEFAULT_PIDS_LIMIT = 512
ALLOW_REGISTER = True
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
DATABASE_URL = "sqlite:///./servermanager.db"
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOCK_DIR = BASE_DIR / "runtime"
FALLBACK_DATA_DIR = str(BASE_DIR / "data" / "users")

AVAILABLE_IMAGES = {
    "pytorch": "lab/pytorch:2.3-cuda12.1",
    "pytorch_old": "lab/pytorch:2.1-cuda11.8",
    "tensorflow": "lab/tensorflow:2.15",
    "base": "lab/base:22.04",
}
