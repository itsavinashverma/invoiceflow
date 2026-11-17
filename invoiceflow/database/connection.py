"""Engine / session management for SQLite (local) and PostgreSQL (hosted)."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow.config import Settings, load_settings
from invoiceflow.database.models import SCHEMA_VERSION, Base, SchemaInfo

logger = logging.getLogger(__name__)


def create_db_engine(database_url: str) -> Engine:
    """Create an engine. Creates the parent directory for file-based SQLite databases."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, future=True)

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_conn, _record) -> None:  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    # Hosted PostgreSQL (e.g. Supabase pooler): validate connections before use.
    return create_engine(url, future=True, pool_pre_ping=True, pool_recycle=300)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_db_engine(load_settings().database_url)


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back and re-raise on error."""
    session = get_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> Engine:
    """Create all tables (idempotent) and record the schema version."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    with session_scope(engine) as session:
        if session.get(SchemaInfo, "schema_version") is None:
            session.add(SchemaInfo(key="schema_version", value=SCHEMA_VERSION))
    logger.info("Database initialised (%s)", engine.url.render_as_string(hide_password=True))
    return engine


def check_connection(engine: Engine | None = None) -> None:
    """Raise if the database cannot be reached."""
    from sqlalchemy import text

    with (engine or get_engine()).connect() as conn:
        conn.execute(text("SELECT 1"))


def describe(settings: Settings | None = None) -> str:
    return (settings or load_settings()).safe_summary()["database"]
