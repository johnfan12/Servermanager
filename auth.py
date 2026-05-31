"""Local Linux account authentication helpers for the simplified server."""

from __future__ import annotations

import grp
import hmac
import logging
import os
import pwd
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

_auth_logger = logging.getLogger(__name__)


def _resolve_secret(env_names: list[str], label: str) -> str:
    """Read a secret from environment variables, falling back to an ephemeral random value."""
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    fallback = secrets.token_hex(32)
    _auth_logger.warning(
        "%s not configured! Generated ephemeral secret. "
        "Tokens will be invalidated on restart and cannot be shared across instances.",
        label,
    )
    return fallback


JWT_SECRET = _resolve_secret(["SIMPLE_JWT_SECRET", "JWT_SECRET"], "JWT_SECRET")
JWT_ALGORITHM = os.environ.get("SIMPLE_JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.environ.get("SIMPLE_JWT_EXPIRE_HOURS", "24"))
PAM_SERVICE = os.environ.get("SIMPLE_PAM_SERVICE", "login")
ALLOW_SYSTEM_USERS = os.environ.get("SIMPLE_ALLOW_SYSTEM_USERS", "false").lower() == "true"
ALLOWED_UID_MIN = int(os.environ.get("SIMPLE_ALLOWED_UID_MIN", "1000"))
ALLOWED_GROUPS = {
    item.strip()
    for item in os.environ.get("SIMPLE_ALLOWED_GROUPS", "").split(",")
    if item.strip()
}
ADMIN_USERS = {
    item.strip()
    for item in os.environ.get("SIMPLE_ADMIN_USERS", "").split(",")
    if item.strip()
}
ADMIN_GROUPS = {
    item.strip()
    for item in os.environ.get("SIMPLE_ADMIN_GROUPS", "sudo,wheel").split(",")
    if item.strip()
}

security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """Authenticated local account."""

    username: str
    is_admin: bool = False


def _local_user(username: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(username)
    except KeyError:
        return None


def _user_groups(username: str, primary_gid: int) -> set[str]:
    groups: set[str] = set()
    try:
        groups.add(grp.getgrgid(primary_gid).gr_name)
    except KeyError:
        pass

    for group in grp.getgrall():
        if username in group.gr_mem:
            groups.add(group.gr_name)
    return groups


def is_admin_username(username: str) -> bool:
    """Return whether the local account should be treated as an admin."""
    user = _local_user(username)
    if user is None:
        return False
    if username in ADMIN_USERS:
        return True
    return bool(_user_groups(username, user.pw_gid) & ADMIN_GROUPS)


def is_local_user_allowed(username: str) -> bool:
    """Return whether a local account is allowed to use the simplified service."""
    user = _local_user(username)
    if user is None:
        return False

    if not ALLOW_SYSTEM_USERS and user.pw_uid < ALLOWED_UID_MIN and username not in ADMIN_USERS:
        return False

    shell = str(user.pw_shell or "")
    if shell.endswith(("nologin", "false")):
        return False

    if ALLOWED_GROUPS and not (_user_groups(username, user.pw_gid) & ALLOWED_GROUPS):
        return False

    return True


def authenticate_local_user(username: str, password: str) -> Principal:
    """Authenticate a username/password pair against the host PAM stack."""
    username = username.strip()
    if not username or not password:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    if not is_local_user_allowed(username):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    try:
        import pam  # type: ignore[import-not-found]
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="PAM authentication is unavailable. Install python-pam or python3-pam.",
        ) from exc

    try:
        if hasattr(pam, "pam"):
            pam_client = pam.pam()
            authenticated = pam_client.authenticate(username, password, service=PAM_SERVICE)
        else:
            authenticated = pam.authenticate(username, password, service=PAM_SERVICE)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid username or password.") from exc

    if not authenticated:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return Principal(username=username, is_admin=is_admin_username(username))


def create_access_token(principal: Principal) -> str:
    """Create a short-lived JWT for the local account."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": principal.username,
        "is_admin": principal.is_admin,
        "exp": expires_at,
        "scope": "simple-servermanager",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> Principal:
    """Read the bearer token and return the current local principal."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token.") from exc

    username = str(payload.get("sub") or "")
    if not is_local_user_allowed(username):
        raise HTTPException(status_code=401, detail="Local account is not allowed.")
    return Principal(username=username, is_admin=is_admin_username(username))


def verify_internal_token(received: str | None, expected: str) -> None:
    """Validate service-to-service requests without leaking timing details."""
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token.")
