from datetime import UTC, date, datetime

import pytest
from sqlalchemy import inspect, text

from invoiceflow.database import migrations
from invoiceflow.database.connection import DatabaseUnavailableError, create_db_engine, init_db
from invoiceflow.database.models import Base, ExtractionRecordRow, PurchaseOrder, SchemaInfo

ALL_VERSIONS = list(range(1, migrations.LATEST_VERSION + 1))
ALL_TABLES = {"schema_info", "extraction_records", "purchase_orders", "processing_runs", "invoices", "processing_events"}


@pytest.fixture
def engine(tmp_path):
    e = create_db_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    yield e
    e.dispose()


def set_version(engine, value: str):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM schema_info WHERE key='schema_version'"))
        conn.execute(text("INSERT INTO schema_info(key, value) VALUES ('schema_version', :v)"), {"v": value})


def insert_extraction_row(engine, record_id="keep-me"):
    with engine.begin() as conn:
        conn.execute(
            ExtractionRecordRow.__table__.insert().values(
                record_id=record_id,
                source="ai",
                status="success",
                extracted_at=datetime(2024, 1, 1, tzinfo=UTC),
                attempts=1,
                raw_response="precious audit data",
            )
        )


def surviving_row(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT raw_response FROM extraction_records WHERE record_id='keep-me'")).scalar()


def test_migration_list_is_consecutive():
    assert [m.version for m in migrations.MIGRATIONS] == list(range(1, len(migrations.MIGRATIONS) + 1))
    assert len(migrations.MIGRATIONS) == migrations.LATEST_VERSION


def test_fresh_database_gets_all_migrations_once(engine):
    assert migrations.migrate(engine) == ALL_VERSIONS
    assert set(inspect(engine).get_table_names()) == ALL_TABLES
    assert migrations.migrate(engine) == []  # nothing left to do


def test_legacy_unversioned_database_is_adopted_without_data_loss(engine):
    # What the first releases created: version marker "0", extraction_records, real data in it.
    SchemaInfo.__table__.create(engine)
    ExtractionRecordRow.__table__.create(engine)
    set_version(engine, "0")
    insert_extraction_row(engine)

    assert migrations.migrate(engine) == ALL_VERSIONS
    assert surviving_row(engine) == "precious audit data"
    with engine.connect() as conn:
        assert migrations.get_schema_version(conn) == migrations.LATEST_VERSION


def test_upgrade_from_v1_keeps_existing_rows(engine):
    with engine.begin() as conn:
        migrations.MIGRATIONS[0].apply(conn)
    set_version(engine, "1")
    insert_extraction_row(engine)
    assert migrations.migrate(engine) == [2, 3, 4]
    assert surviving_row(engine) == "precious audit data"


def test_partially_applied_migration_can_be_repeated(engine):
    migrations.migrate(engine)
    set_version(engine, "1")  # simulate a crash after tables were created but before the version bump
    with engine.begin() as conn:
        conn.execute(
            PurchaseOrder.__table__.insert().values(
                po_number="PO-KEEP",
                vendor_name="v",
                normalized_vendor_name="v",
                currency="USD",
                total_amount="10.00",
                tolerance_percent="0.00",
                po_date=date(2024, 1, 1),
                status="OPEN",
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
    assert migrations.migrate(engine) == [2, 3, 4]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM purchase_orders")).scalar() == 1


def test_newer_database_is_refused_and_untouched(engine):
    SchemaInfo.__table__.create(engine)
    set_version(engine, "99")
    with pytest.raises(migrations.SchemaVersionError, match="99"):
        migrations.migrate(engine)
    assert set(inspect(engine).get_table_names()) == {"schema_info"}  # nothing was created
    with pytest.raises(DatabaseUnavailableError, match=r"newer|version 99"):
        init_db(engine)


def test_unreadable_version_is_refused(engine):
    SchemaInfo.__table__.create(engine)
    set_version(engine, "banana")
    with pytest.raises(migrations.SchemaVersionError, match="banana"):
        migrations.migrate(engine)


def test_failed_migration_rolls_back_completely_and_can_be_retried(engine, monkeypatch):
    migrations.migrate(engine)
    latest = migrations.LATEST_VERSION
    base = migrations.MIGRATIONS

    def boom(conn):
        conn.execute(text("CREATE TABLE future_thing (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("migration bug")

    monkeypatch.setattr(migrations, "MIGRATIONS", (*base, migrations.Migration(latest + 1, "broken", boom)))
    monkeypatch.setattr(migrations, "LATEST_VERSION", latest + 1)
    with pytest.raises(RuntimeError, match="migration bug"):
        migrations.migrate(engine)
    with engine.connect() as conn:
        assert migrations.get_schema_version(conn) == latest  # version not advanced
    assert "future_thing" not in inspect(engine).get_table_names()  # DDL was rolled back too (transactional SQLite)

    fixed = migrations.Migration(latest + 1, "fixed", lambda conn: conn.execute(text("CREATE TABLE future_thing (id INTEGER PRIMARY KEY)")))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*base, fixed))
    assert migrations.migrate(engine) == [latest + 1]


# ------------------------------------------------------------------ v2 -> v3 (decision engine)

LEGACY_INVOICES_V2 = """
CREATE TABLE invoices (
    id INTEGER NOT NULL, processing_run_id INTEGER NOT NULL, invoice_number VARCHAR(128),
    vendor_name VARCHAR(255), normalized_vendor_name VARCHAR(255), vendor_tax_id VARCHAR(64),
    invoice_date DATE, po_number VARCHAR(64), currency VARCHAR(3), subtotal VARCHAR(22),
    tax_total VARCHAR(22), grand_total VARCHAR(22), extraction_json JSON,
    final_status VARCHAR(32) NOT NULL, decision_reasons JSON NOT NULL, content_hash VARCHAR(64),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id), CONSTRAINT uq_invoice_one_per_run UNIQUE (processing_run_id),
    CONSTRAINT ck_invoice_status CHECK (final_status IN ('AUTO_APPROVE_RECOMMENDED', 'HUMAN_REVIEW', 'REJECT_RECOMMENDED', 'FAILED_EXTRACTION', 'APPROVED', 'REJECTED')),
    FOREIGN KEY(processing_run_id) REFERENCES processing_runs (id) ON DELETE CASCADE
)"""


def make_v2_database(engine, statuses):
    """A database exactly as released at schema v2: old invoices table + old statuses + stale vendor key."""
    migrations.migrate(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE invoices"))
        conn.execute(text(LEGACY_INVOICES_V2))
        conn.execute(text("CREATE INDEX ix_invoices_invoice_number ON invoices (invoice_number)"))
        conn.execute(text("DELETE FROM schema_info WHERE key IN ('schema_version', 'decision_lock')"))
        conn.execute(text("INSERT INTO schema_info VALUES ('schema_version', '2')"))
        for i, status in enumerate(statuses, 1):
            conn.execute(
                text(
                    "INSERT INTO processing_runs (id, run_id, original_filename, file_hash, run_status, started_at) "
                    "VALUES (:i, :r, 'a.pdf', :h, 'COMPLETED', '2024-01-01 00:00:00')"
                ),
                {"i": i, "r": f"run{i}", "h": f"{i:064x}"},
            )
            conn.execute(
                text(
                    "INSERT INTO invoices (id, processing_run_id, invoice_number, vendor_name, normalized_vendor_name, grand_total, "
                    "final_status, decision_reasons, created_at) VALUES (:i, :i, :n, 'Vandelay Import Export S.L.', 'vandelay import export s l', "
                    "'12.50', :s, '[\"OLD\"]', '2024-01-01 00:00:00')"
                ),
                {"i": i, "n": f"N{i}", "s": status},
            )


def test_v2_database_is_upgraded_without_losing_invoices(engine):
    legacy = ["AUTO_APPROVE_RECOMMENDED", "HUMAN_REVIEW", "REJECT_RECOMMENDED", "FAILED_EXTRACTION", "APPROVED", "REJECTED"]
    make_v2_database(engine, legacy)
    assert migrations.migrate(engine) == [3, 4]

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT invoice_number, final_status, grand_total, decision_reasons, normalized_vendor_name, decision_explanation FROM invoices ORDER BY id"
            )
        ).all()
        assert [r[1] for r in rows] == ["APPROVED", "REVIEW_REQUIRED", "REVIEW_REQUIRED", "PROCESSING_FAILED", "APPROVED", "REJECTED"]
        assert [r[0] for r in rows] == [f"N{i}" for i in range(1, 7)] and all(r[2] == "12.50" for r in rows)
        assert all("OLD" in r[3] for r in rows)
        assert all(r[4] == "vandelay import export" for r in rows)  # derived vendor key refreshed ("S.L." handled)
        assert all(r[5] is None for r in rows)
        assert conn.execute(text("SELECT value FROM schema_info WHERE key='decision_lock'")).scalar() == "1"

    insp = inspect(engine)
    assert "decision_explanation" in {c["name"] for c in insp.get_columns("invoices")}
    assert {tuple(i["column_names"]) for i in insp.get_indexes("invoices")} >= {
        ("invoice_number",),
        ("po_number",),
        ("content_hash",),
        ("normalized_vendor_name", "invoice_number"),
    }
    with engine.begin() as conn:  # the new vocabulary is accepted, the old one is not
        conn.execute(text("UPDATE invoices SET final_status='DUPLICATE_BLOCKED' WHERE id=1"))
    with pytest.raises(Exception, match="CHECK"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE invoices SET final_status='HUMAN_REVIEW' WHERE id=1"))
    with pytest.raises(Exception, match="FOREIGN KEY"):  # foreign key and cascade survived the rebuild
        with engine.begin() as conn:
            conn.execute(text("UPDATE invoices SET processing_run_id=999 WHERE id=1"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM processing_runs WHERE id=2"))
        assert conn.execute(text("SELECT count(*) FROM invoices")).scalar() == 5


# ------------------------------------------------------------------ v3 -> v4 (pipeline stage statuses)

LEGACY_EVENTS_V3 = """
CREATE TABLE processing_events (
    id INTEGER NOT NULL, processing_run_id INTEGER NOT NULL, stage_name VARCHAR(32) NOT NULL,
    stage_status VARCHAR(16) NOT NULL, message TEXT, started_at DATETIME, completed_at DATETIME, duration_ms INTEGER,
    PRIMARY KEY (id), CONSTRAINT uq_event_run_stage UNIQUE (processing_run_id, stage_name),
    CONSTRAINT ck_event_status CHECK (stage_status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    CONSTRAINT ck_event_duration CHECK (duration_ms IS NULL OR duration_ms >= 0),
    FOREIGN KEY(processing_run_id) REFERENCES processing_runs (id) ON DELETE CASCADE
)"""


def test_v3_stage_events_are_upgraded_keeping_every_row(engine):
    migrations.migrate(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE processing_events"))
        conn.execute(text(LEGACY_EVENTS_V3))
        conn.execute(text("CREATE INDEX ix_processing_events_processing_run_id ON processing_events (processing_run_id)"))
        conn.execute(text("UPDATE schema_info SET value='3' WHERE key='schema_version'"))
        conn.execute(
            text(
                "INSERT INTO processing_runs (id, run_id, original_filename, file_hash, run_status, started_at) "
                "VALUES (1, 'r1', 'a.pdf', :h, 'COMPLETED', '2024-01-01 00:00:00')"
            ),
            {"h": "a" * 64},
        )
        for i, (stage, status) in enumerate(
            [("validate", "SUCCEEDED"), ("extract", "SUCCEEDED"), ("ai", "FAILED"), ("decide", "SKIPPED"), ("x", "RUNNING")], 1
        ):
            conn.execute(
                text(
                    "INSERT INTO processing_events (id, processing_run_id, stage_name, stage_status, message, duration_ms) "
                    "VALUES (:i, 1, :s, :st, :m, 7)"
                ),
                {"i": i, "s": stage, "st": status, "m": f"msg {i}"},
            )

    assert migrations.migrate(engine) == [4]
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT stage_name, stage_status, message, duration_ms FROM processing_events ORDER BY id")).all()
    assert rows == [
        ("validate", "COMPLETED", "msg 1", 7),
        ("extract", "COMPLETED", "msg 2", 7),
        ("ai", "FAILED", "msg 3", 7),
        ("decide", "SKIPPED", "msg 4", 7),
        ("x", "RUNNING", "msg 5", 7),
    ]
    assert ("processing_run_id",) in {tuple(i["column_names"]) for i in inspect(engine).get_indexes("processing_events")}
    with engine.begin() as conn:  # PENDING is now valid, SUCCEEDED is not; cascade still works
        conn.execute(text("INSERT INTO processing_events (id, processing_run_id, stage_name, stage_status) VALUES (9, 1, 'p', 'PENDING')"))
    with pytest.raises(Exception, match="CHECK"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE processing_events SET stage_status='SUCCEEDED' WHERE id=1"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM processing_runs WHERE id=1"))
        assert conn.execute(text("SELECT count(*) FROM processing_events")).scalar() == 0


def test_upgrade_is_not_repeated_and_fresh_databases_skip_it(engine):
    assert migrations.migrate(engine) == ALL_VERSIONS
    with engine.connect() as conn:
        assert migrations._invoices_schema_is_current(conn)
    assert migrations.migrate(engine) == []


def test_interrupted_sqlite_rebuild_leaves_old_data_intact(engine, monkeypatch):
    make_v2_database(engine, ["AUTO_APPROVE_RECOMMENDED"])

    def explode(conn):
        raise RuntimeError("disk full")

    monkeypatch.setattr(migrations, "_refresh_vendor_keys", explode)  # fails after the table swap, inside the transaction
    with pytest.raises(RuntimeError, match="disk full"):
        migrations.migrate(engine)
    with engine.connect() as conn:
        assert migrations.get_schema_version(conn) == 2
        assert conn.execute(text("SELECT final_status FROM invoices")).scalar() == "AUTO_APPROVE_RECOMMENDED"  # untouched
        assert "invoices_rebuild" not in inspect(conn).get_table_names()
    monkeypatch.undo()
    assert migrations.migrate(engine) == [3, 4]


def test_migrations_never_drop_anything(engine, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("drop_all must never be called")

    monkeypatch.setattr(Base.metadata, "drop_all", forbidden)
    migrations.migrate(engine)
    migrations.migrate(engine)
