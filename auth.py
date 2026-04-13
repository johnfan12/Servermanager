"""Authentication and authorization helpers."""

from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    JWT_EXPIRE_HOURS,
    JWT_SECRET,
)
from database import get_db
from models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
SHADOW_EMAIL_DOMAIN = "shadow.local"


def hash_password(password: str) -> str:
    """Hash a plaintext password."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    return pwd_context.verify(plain_password, password_hash)


def build_shadow_password_hash(subject: str) -> str:
    """Create one non-interactive password hash for a node shadow user."""
    return hash_password(f"shadow-only::{subject}::{JWT_SECRET}")


def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
    *,
    is_admin: bool = False,
    email: str | None = None,
) -> str:
    """Create a signed JWT for the given subject."""
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=JWT_EXPIRE_HOURS))
    payload: dict[str, Any] = {"sub": subject, "exp": expire, "is_admin": is_admin}
    if email:
        payload["email"] = email
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _resolve_shadow_email(db: Session, username: str, email: str | None) -> str:
    """Return one safe email for a node shadow user, avoiding unique conflicts."""
    normalized = str(email or "").strip().lower()
    candidate = normalized or f"{username}@{SHADOW_EMAIL_DOMAIN}"
    existing = db.query(User).filter(User.email == candidate).first()
    if existing is None or existing.username == username:
        return candidate
    return f"{username}@{SHADOW_EMAIL_DOMAIN}"


def ensure_shadow_user(
    db: Session,
    username: str,
    *,
    is_admin: bool,
    email: str | None = None,
) -> User:
    """Create or refresh one node shadow user from trusted JWT claims."""
    user = db.query(User).filter(User.username == username).first()
    resolved_email = _resolve_shadow_email(db, username, email)
    changed = False

    if user is None:
        user = User(
            username=username,
            email=resolved_email,
            password_hash=build_shadow_password_hash(username),
            is_admin=is_admin,
        )
        db.add(user)
        changed = True
    else:
        if user.is_admin != is_admin:
            user.is_admin = is_admin
            changed = True
        if resolved_email and user.email != resolved_email:
            user.email = resolved_email
            changed = True

    if changed:
        db.commit()
        db.refresh(user)
    return user


def get_current_user(
    db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)
) -> User:
    """Return the currently authenticated user from the JWT token."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            raise credentials_error
    except JWTError as exc:
        raise credentials_error from exc

    return ensure_shadow_user(
        db,
        str(username),
        is_admin=bool(payload.get("is_admin", username == ADMIN_USERNAME)),
        email=payload.get("email"),
    )


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """Ensure the current user has administrator privileges."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="Administrator privileges are required."
        )
    return current_user


def ensure_default_admin(db: Session) -> None:
    """Create the default administrator account when it does not exist."""
    admin = db.query(User).filter(User.username == ADMIN_USERNAME).first()
    if admin:
        return

    db.add(
        User(
            username=ADMIN_USERNAME,
            password_hash=hash_password(ADMIN_PASSWORD),
            email=f"{ADMIN_USERNAME}@local",
            is_admin=True,
        )
    )
    db.commit()
