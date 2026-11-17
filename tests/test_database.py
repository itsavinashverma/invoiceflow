import pytest
from sqlalchemy import inspect

from invoiceflow.database.connection import check_connection, create_db_engine, init_db, session_scope
from invoiceflow.database.models import SCHEMA_VERSION, SchemaInfo


def test_sqlite_file_database_initialises(tmp_path):
    db = tmp_path / "nested" / "test.db"
    engine = create_db_engine(f"sqlite:///{db.as_posix()}")
    init_db(engine)
    check_connection(engine)
    assert db.exists()
    assert "schema_info" in inspect(engine).get_table_names()
    with session_scope(engine) as s:
        assert s.get(SchemaInfo, "schema_version").value == SCHEMA_VERSION
    init_db(engine)  # idempotent
    engine.dispose()


def test_postgres_engine_uses_psycopg_driver():
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:  # e.g. libpq DLL blocked by a Windows Application Control policy
        pytest.skip(f"psycopg unusable on this machine: {str(exc).splitlines()[0]}")
    engine = create_db_engine("postgresql+psycopg://u:p@localhost:5432/db")  # lazy: no connection made
    assert engine.dialect.driver == "psycopg"
    engine.dispose()
