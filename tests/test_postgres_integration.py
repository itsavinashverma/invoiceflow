"""Opt-in PostgreSQL integration coverage, isolated to a temporary schema.

Set INVOICEFLOW_TEST_POSTGRES_URL (or the individual INVOICEFLOW_TEST_POSTGRES_* connection fields)
to a disposable PostgreSQL database to run these tests with ``python -m pytest -m postgres``. Each test
creates a uniquely named schema, sets it transaction
locally (safe for transaction-pooling connections), and drops only that schema during teardown.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import URL, make_url

from invoiceflow.config import normalize_database_url
from invoiceflow.database.connection import create_db_engine, init_db, session_scope
from invoiceflow.database.models import ProcessingRun, PurchaseOrder


@pytest.fixture
def postgres_engine():
    raw_url = os.environ.get("INVOICEFLOW_TEST_POSTGRES_URL")
    if raw_url:
        url = make_url(normalize_database_url(raw_url))
    elif os.environ.get("INVOICEFLOW_TEST_POSTGRES_HOST"):
        url = URL.create(
            "postgresql+psycopg",
            username=os.environ.get("INVOICEFLOW_TEST_POSTGRES_USER", "invoiceflow"),
            host=os.environ["INVOICEFLOW_TEST_POSTGRES_HOST"],
            port=int(os.environ.get("INVOICEFLOW_TEST_POSTGRES_PORT", "5432")),
            database=os.environ.get("INVOICEFLOW_TEST_POSTGRES_DB", "invoiceflow_test"),
        )
    else:
        pytest.skip("Set INVOICEFLOW_TEST_POSTGRES_URL to run PostgreSQL integration tests")
    if url.get_backend_name() != "postgresql":
        pytest.fail("INVOICEFLOW_TEST_POSTGRES_URL must use PostgreSQL")

    admin = create_db_engine(url.render_as_string(hide_password=False))
    schema = f"invoiceflow_test_{uuid4().hex}"
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        engine = create_db_engine(url.render_as_string(hide_password=False))

        @event.listens_for(engine, "begin")
        def _select_isolated_schema(connection):
            # SET LOCAL is scoped to the active transaction, including on PgBouncer transaction pooling.
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')

        init_db(engine)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


@pytest.mark.postgres
def test_postgres_migrations_money_and_utc_round_trip(postgres_engine):
    from datetime import UTC, date, datetime

    assert set(inspect(postgres_engine).get_table_names()) == {
        "extraction_records",
        "invoices",
        "processing_events",
        "processing_runs",
        "purchase_orders",
        "schema_info",
    }
    with postgres_engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM schema_info WHERE key='schema_version'")).scalar_one() == "4"

    with session_scope(postgres_engine) as session:
        session.add(
            PurchaseOrder(
                po_number="PG-ROUNDTRIP-1",
                vendor_name="Postgres Integration Vendor",
                normalized_vendor_name="postgres integration vendor",
                currency="USD",
                total_amount=Decimal("12345678901234.56"),
                tolerance_percent=Decimal("0.10"),
                po_date=date(2026, 1, 15),
            )
        )
        session.add(
            ProcessingRun(
                run_id="a" * 32,
                original_filename="roundtrip.pdf",
                file_hash="b" * 64,
                run_status="RUNNING",
                started_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            )
        )

    with session_scope(postgres_engine) as session:
        po = session.query(PurchaseOrder).filter_by(po_number="PG-ROUNDTRIP-1").one()
        run = session.query(ProcessingRun).filter_by(run_id="a" * 32).one()
        assert po.total_amount == Decimal("12345678901234.56")
        assert po.tolerance_percent == Decimal("0.10")
        assert run.started_at == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        assert run.started_at.utcoffset().total_seconds() == 0


@pytest.mark.postgres
def test_postgres_serializes_concurrent_duplicate_invoice_processing(postgres_engine, settings, demo, gen_module):
    from fakes import FakeAiProvider, ground_truth

    from invoiceflow.database.models import Invoice
    from invoiceflow.database.seed import seed_purchase_orders
    from invoiceflow.services.pipeline import InvoicePipeline

    with session_scope(postgres_engine) as session:
        seed_purchase_orders(session)

    payload = demo["standard"].path.read_bytes()
    extraction = ground_truth(gen_module, "invoice_standard_digital.pdf")

    def process(index: int):
        pipeline = InvoicePipeline(settings, engine=postgres_engine, ai_provider=FakeAiProvider(extraction))
        return pipeline.process(payload, f"concurrent-{index}.pdf")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(process, (1, 2)))

    assert sorted(result.decision_status for result in results) == ["APPROVED", "DUPLICATE_BLOCKED"]
    with session_scope(postgres_engine) as session:
        statuses = [row.final_status for row in session.query(Invoice).order_by(Invoice.id)]
    assert sorted(statuses) == ["APPROVED", "DUPLICATE_BLOCKED"]
