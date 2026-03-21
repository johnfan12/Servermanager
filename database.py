"""PostgreSQL database setup helpers."""

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)
Base = declarative_base()


def init_db() -> None:
    """Verify that the database is reachable.

    Schema creation is managed by Alembic migrations.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def get_db() -> Generator[Session, None, None]:
    """Provide a database session for request handling."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
