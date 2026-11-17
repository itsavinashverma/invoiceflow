"""Engine / session management for SQLite (local default) and PostgreSQL (``DATABASE_URL``).

Connection problems surface as :class:`DatabaseUnavailableError` with a message that is safe to
show: it names the host and database but never the password.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow.config import Settings, load_settings
from invoiceflow.database import migrations

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10


class DatabaseUnavailableError(RuntimeError):
    """The database cannot be reached or initialised. ``str(exc)`` is safe to display."""


def _safe_reason(exc: BaseException, url_text: str) -> str:
    """First line of the underlying error with anything credential-like removed."""
    reason = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    reason = re.sub(r"(?i)(password|pwd)\s*[=:]\s*\S+", r"\1=***", reason)
    reason = re.sub(r"://[^@\s/]*@", "://***@", reason)
    password = make_url(url_text).password
    if password:
        reason = reason.replace(password, "***")
    return reason[:300]


def safe_error_reason(exc: BaseException, engine: Engine) -> str:
    """Credential-free one-line reason for any database exception (for scripts and UI)."""
    return _safe_reason(exc, engine.url.render_as_string(hide_password=False))


def describe_target(url_text: str) -> str:
    """``postgresql://host:5432/db`` style description without credentials."""
    url = make_url(url_text)
    if url.get_backend_name() == "sqlite":
        return f"SQLite file {url.database}" if url.database not in (None, "", ":memory:") else "in-memory SQLite"
    host = f"{url.host}:{url.port}" if url.port else str(url.host)
    return f"{url.get_backend_name()} at {host}/{url.database}"


def create_db_engine(database_url: str) -> Engine:
    """Create an engine. Creates the parent directory for file-based SQLite databases."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            try:
                Path(url.database).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DatabaseUnavailableError(f"Cannot create the folder for the SQLite database: {exc.strerror or exc}") from exc
        engine = create_engine(url, future=True, connect_args={"timeout": 15})

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_conn, _record) -> None:
            # Take transaction control away from pysqlite (SQLAlchemy's documented recipe) so that DDL is
            # transactional too: a failed migration rolls back instead of leaving half a schema.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin(conn) -> None:
            # IMMEDIATE takes SQLite's write lock up front, so concurrent writers queue (up to the busy
            # timeout) instead of failing instantly when a read-then-write transaction cannot be upgraded.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    # Hosted PostgreSQL (e.g. Supabase pooler): validate connections before use, fail fast when unreachable.
    connect_args: dict = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    if url.get_driver_name() == "psycopg":
        # Supabase's transaction pooler (PgBouncer, port 6543) cannot keep server-side prepared
        # statements across pooled connections: "prepared statement _pg3_0 already exists".
        connect_args["prepare_threshold"] = None
    return create_engine(url, future=True, pool_pre_ping=True, pool_recycle=300, connect_args=connect_args)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_db_engine(load_settings().database_url)


def reset_engine_cache() -> None:
    """Drop the cached engine (tests, or after settings change)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


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


def check_connection(engine: Engine | None = None) -> None:
    """Raise :class:`DatabaseUnavailableError` if the database cannot be reached."""
    engine = engine or get_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        url_text = engine.url.render_as_string(hide_password=False)
        raise DatabaseUnavailableError(
            f"Could not connect to the {describe_target(url_text)}: {_safe_reason(exc, url_text)}. "
            "Check DATABASE_URL, the network/IP allow-list and that the database is running."
        ) from None


def init_db(engine: Engine | None = None) -> Engine:
    """Connect, then migrate the schema to the latest version (safe to call on every start)."""
    engine = engine or get_engine()
    check_connection(engine)
    try:
        applied = migrations.migrate(engine)
    except migrations.SchemaVersionError as exc:
        raise DatabaseUnavailableError(str(exc)) from exc
    except SQLAlchemyError as exc:
        url_text = engine.url.render_as_string(hide_password=False)
        raise DatabaseUnavailableError(f"Database migration failed: {_safe_reason(exc, url_text)}") from None
    logger.info(
        "Database ready (%s), schema v%d%s",
        describe_target(engine.url.render_as_string(hide_password=False)),
        migrations.LATEST_VERSION,
        f", applied migrations {applied}" if applied else "",
    )
    return engine


def describe(settings: Settings | None = None) -> str:
    return (settings or load_settings()).safe_summary()["database"]
