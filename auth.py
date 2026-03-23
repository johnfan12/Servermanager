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
    GPU_HOURS_DEFAULT_QUOTA,
    JWT_EXPIRE_HOURS,
    JWT_SECRET,
)
from database import get_db
from models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    """Hash a plaintext password."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(subject: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT for the given subject."""
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=JWT_EXPIRE_HOURS))
    payload: dict[str, Any] = {"sub": subject, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


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

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise credentials_error
    return user


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
            gpu_hours_quota=GPU_HOURS_DEFAULT_QUOTA,
        )
    )
    db.commit()
