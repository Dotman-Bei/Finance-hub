"""Engine and session factory shared by every service (build.md §2).

`pool_pre_ping` matters here: services are long-lived and Postgres will drop
idle connections, so without it the first query after a quiet period fails.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    echo=settings.db_echo,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope — commits on success, rolls back on any exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: `db: Session = Depends(get_session)`."""
    with session_scope() as session:
        yield session


def healthcheck() -> bool:
    """True when Postgres answers. Used by service /health endpoints."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


__all__ = ["engine", "SessionLocal", "session_scope", "get_session", "healthcheck"]
