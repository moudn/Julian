from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def normalize_db_url(url: str) -> str:
    """Accept the URLs hosts hand out and make them SQLAlchemy-ready.

    Supabase/Heroku give `postgres://...`; SQLAlchemy needs an explicit
    driver. Map bare postgres URLs to the psycopg (v3) driver.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine():
    settings = get_settings()
    url = normalize_db_url(settings.database_url)
    connect_args = {}
    kwargs = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    else:
        # Managed Postgres (Supabase et al.) closes idle connections; verify
        # each one before use and recycle before the server would drop it.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
    return create_engine(url, connect_args=connect_args, **kwargs)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # Import models so their tables are registered on Base.metadata
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
