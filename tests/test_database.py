from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import LargeBinary, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from invoiceflow.database import migrations
from invoiceflow.database.connection import (
    DatabaseUnavailableError,
    check_connection,
    create_db_engine,
    init_db,
    session_scope,
)
from invoiceflow.database.models import (
    Base,
    Invoice,
    ProcessingEvent,
    ProcessingRun,
    PurchaseOrder,
)

SHA = "a" * 64
EXPECTED_TABLES = {"schema_info", "extraction_records", "purchase_orders", "processing_runs", "invoices", "processing_events"}


def make_po(**kw):
    values = dict(
        po_number="PO-1",
        vendor_name="Acme Ltd",
        normalized_vendor_name="acme",
        currency="USD",
        total_amount=Decimal("100.00"),
        tolerance_percent=Decimal("2.00"),
        po_date=date(2024, 1, 1),
        status="OPEN",
    )
    values.update(kw)
    return PurchaseOrder(**values)


def make_run(session, run_id="r1"):
    run = ProcessingRun(run_id=run_id, original_filename="a.pdf", file_hash=SHA, run_status="RUNNING")
    session.add(run)
    session.flush()
    return run


# ------------------------------------------------------------------ creation


def test_tables_created_with_expected_columns(db_engine):
    insp = inspect(db_engine)
    assert set(insp.get_table_names()) == EXPECTED_TABLES
    cols = lambda t: {c["name"] for c in insp.get_columns(t)}  # noqa: E731
    assert cols("purchase_orders") == {
        "id",
        "po_number",
        "vendor_name",
        "normalized_vendor_name",
        "currency",
        "total_amount",
        "tolerance_percent",
        "po_date",
        "status",
        "created_at",
    }
    assert cols("processing_runs") == {
        "id",
        "run_id",
        "original_filename",
        "file_hash",
        "current_stage",
        "run_status",
        "started_at",
        "completed_at",
        "overall_error",
    }
    assert cols("invoices") == {
        "id",
        "processing_run_id",
        "invoice_number",
        "vendor_name",
        "normalized_vendor_name",
        "vendor_tax_id",
        "invoice_date",
        "po_number",
        "currency",
        "subtotal",
        "tax_total",
        "grand_total",
        "extraction_json",
        "final_status",
        "decision_reasons",
        "content_hash",
        "created_at",
        "decision_explanation",
    }
    assert cols("processing_events") == {
        "id",
        "processing_run_id",
        "stage_name",
        "stage_status",
        "message",
        "started_at",
        "completed_at",
        "duration_ms",
    }


def test_init_is_idempotent_and_versioned(db_engine):
    assert init_db(db_engine) is db_engine
    with db_engine.connect() as conn:
        assert migrations.get_schema_version(conn) == migrations.LATEST_VERSION


def test_indexes_exist(db_engine):
    insp = inspect(db_engine)
    indexed = lambda t: {tuple(i["column_names"]) for i in insp.get_indexes(t)}  # noqa: E731
    assert ("normalized_vendor_name",) in indexed("purchase_orders")
    assert {("file_hash",), ("run_status",), ("started_at",)} <= indexed("processing_runs")
    assert {("invoice_number",), ("po_number",), ("content_hash",), ("normalized_vendor_name", "invoice_number")} <= indexed("invoices")
    assert ("processing_run_id",) in indexed("processing_events")


def test_no_binary_columns_so_pdfs_cannot_be_stored():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            assert not isinstance(col.type, LargeBinary), f"{table.name}.{col.name} can hold binary data"


# ------------------------------------------------------------------ constraints


def test_po_number_unique(db_engine):
    with pytest.raises(IntegrityError):
        with session_scope(db_engine) as s:
            s.add_all([make_po(), make_po(vendor_name="Other")])


@pytest.mark.parametrize("bad", [dict(status="PENDING"), dict(currency="US"), dict(total_amount=Decimal("-1.00"))])
def test_po_check_constraints(db_engine, bad):
    with pytest.raises(IntegrityError):
        with session_scope(db_engine) as s:
            s.add(make_po(**bad))


def test_run_constraints(db_engine):
    with pytest.raises(IntegrityError):
        with session_scope(db_engine) as s:
            s.add(ProcessingRun(run_id="x", original_filename="a.pdf", file_hash=SHA, run_status="WEIRD"))
    with pytest.raises(IntegrityError):
        with session_scope(db_engine) as s:
            make_run(s, "dup")
            make_run(s, "dup")


def test_foreign_keys_enforced_and_cascade(db_engine):
    with pytest.raises(IntegrityError):
        with session_scope(db_engine) as s:
            s.add(ProcessingEvent(processing_run_id=999, stage_name="x", stage_status="RUNNING"))
    with session_scope(db_engine) as s:
        run = make_run(s)
        s.add(ProcessingEvent(processing_run_id=run.id, stage_name="extract", stage_status="RUNNING"))
        s.add(Invoice(processing_run_id=run.id, final_status="REVIEW_REQUIRED", decision_reasons=[]))
    with session_scope(db_engine) as s:
        s.delete(s.query(ProcessingRun).one())
    with session_scope(db_engine) as s:
        assert s.query(ProcessingEvent).count() == 0 and s.query(Invoice).count() == 0


def test_event_and_invoice_constraints(db_engine):
    with session_scope(db_engine) as s:
        run = make_run(s)
        run_pk = run.id
        s.add(ProcessingEvent(processing_run_id=run_pk, stage_name="extract", stage_status="RUNNING"))
        s.add(Invoice(processing_run_id=run_pk, final_status="REVIEW_REQUIRED", decision_reasons=[]))
    for bad in (
        ProcessingEvent(processing_run_id=run_pk, stage_name="extract", stage_status="RUNNING"),  # run+stage unique
        ProcessingEvent(processing_run_id=run_pk, stage_name="other", stage_status="DONE"),  # bad status
        ProcessingEvent(processing_run_id=run_pk, stage_name="other", stage_status="FAILED", duration_ms=-5),
        Invoice(processing_run_id=run_pk, final_status="REVIEW_REQUIRED", decision_reasons=[]),  # one per run
    ):
        with pytest.raises(IntegrityError):
            with session_scope(db_engine) as s:
                s.add(bad)
    with pytest.raises(IntegrityError):
        with session_scope(db_engine) as s:
            run2 = make_run(s, "r2")
            s.add(Invoice(processing_run_id=run2.id, final_status="PAID", decision_reasons=[]))


# ------------------------------------------------------------------ exact money / UTC time


def test_money_is_exact_on_sqlite(db_engine):
    amount = Decimal("12345678901234.56")  # float cannot hold this exactly
    with session_scope(db_engine) as s:
        s.add(make_po(total_amount=amount, tolerance_percent=Decimal("0.10")))
    with session_scope(db_engine) as s:
        po = s.query(PurchaseOrder).one()
        assert po.total_amount == amount and isinstance(po.total_amount, Decimal)
        assert po.tolerance_percent == Decimal("0.10") and str(po.tolerance_percent) == "0.10"
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT total_amount FROM purchase_orders")).scalar() == "12345678901234.56"


@pytest.mark.parametrize("bad, match", [(1.5, "never float"), (Decimal("1.005"), "decimal places"), (Decimal("1" * 17), "exceeds")])
def test_money_rejects_floats_and_silent_rounding(db_engine, bad, match):
    with pytest.raises(StatementError, match=match):
        with session_scope(db_engine) as s:
            s.add(make_po(total_amount=bad))


def test_datetimes_are_utc_aware(db_engine):
    stamp = datetime(2024, 5, 1, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with session_scope(db_engine) as s:
        s.add(ProcessingRun(run_id="t", original_filename="a.pdf", file_hash=SHA, run_status="RUNNING", started_at=stamp))
    with session_scope(db_engine) as s:
        got = s.query(ProcessingRun).one().started_at
        assert got.tzinfo is not None and got == stamp and got.utcoffset() == timedelta(0)
    with pytest.raises(StatementError, match="Naive"):
        with session_scope(db_engine) as s:
            s.add(ProcessingRun(run_id="n", original_filename="a.pdf", file_hash=SHA, started_at=datetime(2024, 1, 1)))


# ------------------------------------------------------------------ transactions


def test_session_scope_rolls_back_on_error(db_engine):
    with pytest.raises(RuntimeError):
        with session_scope(db_engine) as s:
            s.add(make_po())
            s.flush()
            raise RuntimeError("boom")
    with session_scope(db_engine) as s:
        assert s.query(PurchaseOrder).count() == 0


# ------------------------------------------------------------------ connection handling


def test_unreachable_postgres_fails_gracefully_without_leaking_password(monkeypatch):
    monkeypatch.setattr("invoiceflow.database.connection.CONNECT_TIMEOUT_SECONDS", 1)  # keep the test fast
    engine = create_db_engine("postgresql+psycopg://svc_user:S3cr3tPassw0rd@127.0.0.1:1/appdb")
    with pytest.raises(DatabaseUnavailableError) as exc:
        init_db(engine)
    msg = str(exc.value)
    assert "127.0.0.1:1/appdb" in msg and "DATABASE_URL" in msg
    assert "S3cr3tPassw0rd" not in msg and "svc_user" not in msg
    engine.dispose()


def test_check_connection_ok(db_engine):
    check_connection(db_engine)


def test_unwritable_sqlite_location_is_reported(tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    with pytest.raises(DatabaseUnavailableError, match="SQLite"):
        create_db_engine(f"sqlite:///{(blocker / 'sub' / 'db.sqlite').as_posix()}")


def test_postgres_engine_uses_psycopg_driver_and_pooler_safe_settings():
    engine = create_db_engine("postgresql+psycopg://u:p@localhost:5432/db")  # lazy: no connection made
    assert engine.dialect.driver == "psycopg"
    assert engine.pool._pre_ping
    engine.dispose()


def test_prepared_statements_disabled_for_pooler(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kw):
        captured.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr("invoiceflow.database.connection.create_engine", fake_create_engine)
    with pytest.raises(RuntimeError):
        create_db_engine("postgresql+psycopg://u:p@h:6543/db")
    assert captured["connect_args"]["prepare_threshold"] is None
    assert captured["connect_args"]["connect_timeout"] == 10
