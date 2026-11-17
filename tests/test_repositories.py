from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest

from invoiceflow import constants
from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import create_db_engine, init_db, session_scope
from invoiceflow.database.models import Invoice, ProcessingEvent, ProcessingRun
from invoiceflow.schemas import InvoiceExtraction, LineItemExtraction, NormalizedInvoice, PurchaseOrderData
from invoiceflow.services.normalizer import compute_content_hash, normalize_extraction

_seq = count(1)


def file_hash() -> str:
    return f"{next(_seq):064x}"


def invoice(**kw) -> NormalizedInvoice:
    values = dict(
        invoice_number="INV-001",
        vendor_name="Acme Industrial Supplies Ltd",
        vendor_tax_id="US-47-1",
        invoice_date=date(2024, 9, 20),
        purchase_order_number="PO-1",
        currency="USD",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("7.00"),
        grand_total=Decimal("107.00"),
    )
    values.update(kw)
    return NormalizedInvoice(**values)


def store(session, norm=None, status=constants.DECISION_REVIEW_REQUIRED, reasons=None, name="inv.pdf"):
    run = repo.create_processing_run(session, name, file_hash())
    inv = repo.save_invoice(session, run.run_id, norm or invoice(), final_status=status, decision_reasons=reasons)
    return run, inv


# ============================================================================ purchase orders


def po(**kw) -> PurchaseOrderData:
    values = dict(
        po_number="PO-1",
        vendor_name="Acme Industrial Supplies Ltd",
        currency="USD",
        total_amount=Decimal("1216.00"),
        tolerance_percent=Decimal("2.00"),
        po_date=date(2024, 9, 2),
    )
    values.update(kw)
    return PurchaseOrderData(**values)


def test_create_if_missing_and_fetch(db):
    row, created = repo.create_purchase_order_if_missing(db, po())
    assert created and row.id and row.created_at.tzinfo
    again, created2 = repo.create_purchase_order_if_missing(db, po(total_amount=Decimal("1.00")))
    assert not created2 and again.id == row.id and again.total_amount == Decimal("1216.00")  # not overwritten
    assert repo.get_purchase_order(db, "  po-1 ").id == row.id  # case/whitespace-insensitive
    assert repo.get_purchase_order(db, "PO-404") is None
    assert repo.count_purchase_orders(db) == 1


def test_list_purchase_orders_filters(db):
    repo.create_purchase_order_if_missing(db, po(po_number="PO-1"))
    repo.create_purchase_order_if_missing(db, po(po_number="PO-2", vendor_name="ACME Industrial Supplies Limited", status="CLOSED"))
    repo.create_purchase_order_if_missing(db, po(po_number="PO-3", vendor_name="Globex", currency="eur"))
    assert [p.po_number for p in repo.list_purchase_orders(db)] == ["PO-1", "PO-2", "PO-3"]
    assert [p.po_number for p in repo.list_purchase_orders(db, status="CLOSED")] == ["PO-2"]
    assert [p.po_number for p in repo.list_purchase_orders(db, vendor="acme industrial supplies inc.")] == ["PO-1", "PO-2"]
    assert [p.po_number for p in repo.list_purchase_orders(db, currency="eur")] == ["PO-3"]


def test_po_input_is_validated():
    for bad in (
        dict(currency="US"),
        dict(total_amount=Decimal("-1")),
        dict(total_amount=12.5),
        dict(tolerance_percent=Decimal("101")),
        dict(status="PENDING"),
        dict(po_number="  "),
    ):
        with pytest.raises(ValueError):
            po(**bad)


# ============================================================================ runs and stages


def test_create_run(db):
    run = repo.create_processing_run(db, "C:\\Users\\me\\..\\docs\\Invoice\x00 1.pdf", "A" * 64)
    assert len(run.run_id) == 32 and run.run_status == "RUNNING" and run.current_stage is None
    assert run.original_filename == "Invoice 1.pdf" and run.file_hash == "a" * 64
    assert run.started_at.tzinfo is not None and run.completed_at is None
    assert repo.get_run(db, run.run_id).id == run.id
    assert repo.create_processing_run(db, "x.pdf", file_hash()).run_id != run.run_id
    assert repo.create_processing_run(db, None, file_hash()).original_filename == "unnamed.pdf"
    assert len(repo.create_processing_run(db, "n" * 400 + ".pdf", file_hash()).original_filename) == 255


@pytest.mark.parametrize("bad", ["", "xyz", "a" * 63, "g" * 64])
def test_create_run_requires_sha256(db, bad):
    with pytest.raises(repo.RepositoryError):
        repo.create_processing_run(db, "a.pdf", bad)


def test_stage_lifecycle_with_exact_duration(db, monkeypatch):
    t0 = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    clock = iter([t0, t0 + timedelta(seconds=1, milliseconds=500)])
    run = repo.create_processing_run(db, "a.pdf", file_hash())
    monkeypatch.setattr(repo, "utcnow", lambda: next(clock))

    ev = repo.update_stage(db, run.run_id, "extract_text", constants.STAGE_RUNNING, "reading")
    assert ev.stage_status == "RUNNING" and ev.started_at == t0 and ev.completed_at is None and ev.duration_ms is None
    assert run.current_stage == "extract_text"

    ev = repo.update_stage(db, run.run_id, "extract_text", constants.STAGE_COMPLETED)
    assert ev.stage_status == "COMPLETED" and ev.duration_ms == 1500 and ev.completed_at == t0 + timedelta(milliseconds=1500)
    assert ev.message == "reading"  # message kept when not supplied
    assert db.query(ProcessingEvent).count() == 1  # one row per run+stage, updated in place


def test_events_ordered_failed_stage_and_run_completion(db):
    run = repo.create_processing_run(db, "a.pdf", file_hash())
    for stage in ("validate", "extract_text", "ai_extraction"):
        repo.update_stage(db, run.run_id, stage, constants.STAGE_RUNNING)
        repo.update_stage(
            db,
            run.run_id,
            stage,
            constants.STAGE_COMPLETED if stage != "ai_extraction" else constants.STAGE_FAILED,
            None if stage != "ai_extraction" else "Gemini unavailable",
        )
    repo.update_stage(db, run.run_id, "decision", constants.STAGE_SKIPPED, "no data")
    events = repo.list_events(db, run.run_id)
    assert [e.stage_name for e in events] == ["validate", "extract_text", "ai_extraction", "decision"]
    assert [e.stage_status for e in events] == ["COMPLETED", "COMPLETED", "FAILED", "SKIPPED"]
    assert all(e.duration_ms is not None and e.duration_ms >= 0 for e in events)
    assert events[2].message == "Gemini unavailable"
    assert run.current_stage == "ai_extraction"  # a skipped stage never ran, so it does not become the current stage

    done = repo.complete_run(db, run.run_id, constants.RUN_FAILED, "AI extraction failed")
    assert done.run_status == "FAILED" and done.overall_error == "AI extraction failed" and done.completed_at.tzinfo


def test_complete_run_rules(db):
    run = repo.create_processing_run(db, "a.pdf", file_hash())
    with pytest.raises(repo.RepositoryError):
        repo.complete_run(db, run.run_id, constants.RUN_FAILED)  # failure needs a reason
    with pytest.raises(repo.RepositoryError):
        repo.complete_run(db, run.run_id, constants.RUN_RUNNING)
    ok = repo.complete_run(db, run.run_id, constants.RUN_COMPLETED, "ignored")
    assert ok.run_status == "COMPLETED" and ok.overall_error is None


def test_unknown_run_and_bad_status_rejected(db):
    with pytest.raises(repo.RepositoryError, match="Unknown"):
        repo.update_stage(db, "nope", "x", constants.STAGE_RUNNING)
    with pytest.raises(repo.RepositoryError, match="Unknown"):
        repo.list_events(db, "nope")
    run = repo.create_processing_run(db, "a.pdf", file_hash())
    with pytest.raises(repo.RepositoryError):
        repo.update_stage(db, run.run_id, "x", "DONE")


# ============================================================================ invoices


def test_save_invoice_stores_exact_fields(db):
    norm = invoice(grand_total=Decimal("12345678.90"), subtotal=Decimal("0.10"), tax_total=Decimal("0.20"))
    run, inv = store(db, norm, constants.DECISION_APPROVED, ["ALL_CHECKS_PASSED"])
    db.expire_all()
    got = db.get(Invoice, inv.id)
    assert got.processing_run_id == run.id and got.final_status == "APPROVED"
    assert got.grand_total == Decimal("12345678.90") and got.subtotal + got.tax_total == Decimal("0.30")
    assert got.normalized_vendor_name == "acme industrial supplies" and got.po_number == "PO-1"
    assert got.decision_reasons == ["ALL_CHECKS_PASSED"] and got.created_at.tzinfo
    assert got.content_hash == compute_content_hash("Acme Industrial Supplies Ltd", "INV-001", Decimal("12345678.90"), date(2024, 9, 20))
    assert got.extraction_json["invoice_number"] == "INV-001" and got.extraction_json["grand_total"] == "12345678.90"
    assert db.get(ProcessingRun, run.id).invoice.id == got.id


def test_save_invoice_from_real_extraction_pipeline_objects(db):
    ex = InvoiceExtraction(
        invoice_number="X-9",
        vendor_name="Globex IT Services LLC",
        invoice_date="2024-10-01",
        currency="usd",
        subtotal="12,300.00",
        tax_total="1014.75",
        grand_total="13314.75",
        line_items=[LineItemExtraction(description="a", quantity="1", unit_price="1", line_total="1")],
    )
    norm, _ = normalize_extraction(ex)
    _, inv = store(db, norm)
    assert (inv.currency, inv.subtotal, inv.grand_total, inv.invoice_date) == (
        "USD",
        Decimal("12300.00"),
        Decimal("13314.75"),
        date(2024, 10, 1),
    )


def test_resaving_same_run_updates_instead_of_duplicating(db):
    run, first = store(db, invoice(grand_total=Decimal("1.00")))
    second = repo.save_invoice(db, run.run_id, invoice(grand_total=Decimal("2.00")), final_status=constants.DECISION_APPROVED)
    assert second.id == first.id and db.query(Invoice).count() == 1
    assert second.grand_total == Decimal("2.00") and second.final_status == "APPROVED"


def test_failed_extraction_can_be_recorded_without_data(db):
    run = repo.create_processing_run(db, "a.pdf", file_hash())
    inv = repo.save_invoice(db, run.run_id, None, final_status=constants.DECISION_PROCESSING_FAILED, decision_reasons=["AI_UNAVAILABLE"])
    assert inv.invoice_number is None and inv.grand_total is None and inv.content_hash is None
    assert inv.decision_reasons == ["AI_UNAVAILABLE"]


def test_save_invoice_validation(db):
    run = repo.create_processing_run(db, "a.pdf", file_hash())
    with pytest.raises(repo.RepositoryError):
        repo.save_invoice(db, run.run_id, invoice(), final_status="MAYBE")
    with pytest.raises(repo.RepositoryError, match="Unknown"):
        repo.save_invoice(db, "nope", invoice(), final_status=constants.DECISION_REVIEW_REQUIRED)


def test_update_invoice_decision(db):
    run, inv = store(db)
    got = repo.update_invoice_decision(db, run.run_id, constants.DECISION_APPROVED, ["APPROVED_BY_REVIEWER"])
    assert got.final_status == "APPROVED" and got.decision_reasons == ["APPROVED_BY_REVIEWER"]
    other = repo.create_processing_run(db, "b.pdf", file_hash())
    with pytest.raises(repo.RepositoryError, match="no saved invoice"):
        repo.update_invoice_decision(db, other.run_id, constants.DECISION_APPROVED)


def test_deleting_a_run_removes_its_events_and_invoice(db):
    run, _ = store(db)
    repo.update_stage(db, run.run_id, "validate", constants.STAGE_RUNNING)
    db.delete(run)
    db.flush()
    assert db.query(Invoice).count() == 0 and db.query(ProcessingEvent).count() == 0


# ============================================================================ approved invoices per PO


def test_approved_invoices_for_po(db):
    _, a = store(db, invoice(invoice_number="A"), constants.DECISION_APPROVED)
    _, b = store(db, invoice(invoice_number="B"), constants.DECISION_APPROVED)
    store(db, invoice(invoice_number="C"), constants.DECISION_REVIEW_REQUIRED)
    store(db, invoice(invoice_number="D"), constants.DECISION_DUPLICATE_BLOCKED)
    store(db, invoice(invoice_number="E"), constants.DECISION_REJECTED)
    store(db, invoice(invoice_number="F"), constants.DECISION_PROCESSING_FAILED)
    store(db, invoice(invoice_number="G", purchase_order_number="PO-2"), constants.DECISION_APPROVED)
    store(db, invoice(invoice_number="H", purchase_order_number=None), constants.DECISION_APPROVED)
    got = repo.get_approved_invoices_for_po(db, " po-1 ")
    assert [i.invoice_number for i in got] == ["A", "B"]
    assert sum(i.grand_total for i in got) == Decimal("214.00")
    assert repo.get_approved_invoices_for_po(db, "PO-404") == []


# ============================================================================ search


@pytest.fixture
def history(db):
    store(db, invoice(invoice_number="ACME-100", invoice_date=date(2024, 9, 1)), constants.DECISION_APPROVED)
    store(
        db,
        invoice(invoice_number="ACME-200", invoice_date=date(2024, 9, 15), purchase_order_number="PO-2"),
        constants.DECISION_REVIEW_REQUIRED,
    )
    store(
        db,
        invoice(
            invoice_number="GX-5589",
            vendor_name="Globex IT Services LLC",
            vendor_tax_id="US-74",
            invoice_date=date(2024, 10, 1),
            purchase_order_number="PO-3",
        ),
        constants.DECISION_DUPLICATE_BLOCKED,
    )
    store(db, invoice(invoice_number="100%_OFF", vendor_name="Weird & Co", invoice_date=None), constants.DECISION_REVIEW_REQUIRED)
    return db


def numbers(rows):
    return sorted(r.invoice_number for r in rows)


def test_search_filters(history):
    s = history
    assert len(repo.search_invoices(s)) == 4
    assert numbers(repo.search_invoices(s, invoice_number="acme")) == ["ACME-100", "ACME-200"]
    assert numbers(repo.search_invoices(s, invoice_number=" 200 ")) == ["ACME-200"]
    assert numbers(repo.search_invoices(s, vendor="globex")) == ["GX-5589"]
    assert numbers(repo.search_invoices(s, po_number="po-2")) == ["ACME-200"]
    assert numbers(repo.search_invoices(s, status=constants.DECISION_DUPLICATE_BLOCKED)) == ["GX-5589"]
    assert numbers(repo.search_invoices(s, date_from=date(2024, 9, 10), date_to=date(2024, 9, 30))) == ["ACME-200"]
    assert numbers(repo.search_invoices(s, date_from=date(2024, 9, 10))) == ["ACME-200", "GX-5589"]
    assert numbers(repo.search_invoices(s, vendor="globex", status=constants.DECISION_APPROVED)) == []


def test_search_vendor_matches_name_variants(history):
    assert numbers(repo.search_invoices(history, vendor="Acme Industrial Supplies Limited")) == ["ACME-100", "ACME-200"]


def test_search_treats_wildcards_literally(history):
    assert numbers(repo.search_invoices(history, invoice_number="100%")) == ["100%_OFF"]
    assert numbers(repo.search_invoices(history, invoice_number="%")) == ["100%_OFF"]  # not "match everything"
    assert repo.search_invoices(history, invoice_number="ACME_100") == []  # '_' is not a wildcard


def test_search_pagination_and_order(history):
    everything = repo.search_invoices(history)
    assert [i.id for i in everything] == sorted((i.id for i in everything), reverse=True)  # newest first
    page1 = repo.search_invoices(history, limit=3)
    page2 = repo.search_invoices(history, limit=3, offset=3)
    assert len(page1) == 3 and len(page2) == 1 and {i.id for i in page1}.isdisjoint(i.id for i in page2)
    with pytest.raises(repo.RepositoryError):
        repo.search_invoices(history, limit=0)


# ============================================================================ duplicates


def test_duplicate_invoice_numbers(db):
    run, original = store(db, invoice(invoice_number="INV-001"))
    new_run = repo.create_processing_run(db, "again.pdf", file_hash())

    def dup(**kw):
        args = dict(vendor_name="Acme Industrial Supplies Ltd", invoice_number="INV-001", exclude_run_id=new_run.run_id)
        args.update(kw)
        return repo.find_duplicate_invoices(db, **args)

    assert [i.id for i in dup()] == [original.id]
    assert [i.id for i in dup(vendor_name="ACME Industrial Supplies, Limited", invoice_number=" inv-001 ")] == [original.id]
    assert [i.id for i in dup(vendor_name="Totally Different Name", vendor_tax_id="us-47-1")] == [original.id]  # tax id match
    assert dup(vendor_name="Globex") == []  # same number, different vendor: not a duplicate
    assert dup(invoice_number="INV-002") == []
    assert dup(invoice_number=None) == [] and dup(invoice_number="  ") == []
    assert dup(vendor_name=None) == []
    assert (
        repo.find_duplicate_invoices(db, vendor_name="Acme Industrial Supplies Ltd", invoice_number="INV-001", exclude_run_id=run.run_id)
        == []
    )


def test_content_hash_and_file_hash_lookups(db):
    h = "b" * 64
    run = repo.create_processing_run(db, "a.pdf", h)
    inv = repo.save_invoice(db, run.run_id, invoice(), final_status=constants.DECISION_REVIEW_REQUIRED)
    again = repo.create_processing_run(db, "b.pdf", h)
    assert [i.id for i in repo.find_invoices_by_content_hash(db, inv.content_hash)] == [inv.id]
    assert repo.find_invoices_by_content_hash(db, inv.content_hash, exclude_run_id=run.run_id) == []
    assert [r.id for r in repo.find_runs_by_file_hash(db, h.upper(), exclude_run_id=again.run_id)] == [run.id]
    assert repo.find_runs_by_file_hash(db, "c" * 64) == []


# ============================================================================ dashboard


def test_dashboard_stats(db, monkeypatch):
    now = datetime(2024, 11, 1, 12, 0, tzinfo=UTC)
    clock = {"t": now - timedelta(days=30)}
    monkeypatch.setattr(repo, "utcnow", lambda: clock["t"])

    def finish(run, seconds, status=constants.RUN_COMPLETED, error=None):
        clock["t"] = run.started_at + timedelta(seconds=seconds)
        repo.complete_run(db, run.run_id, status, error)

    # old run (outside the 7-day window), 10 s
    old = repo.create_processing_run(db, "old.pdf", file_hash())
    repo.save_invoice(db, old.run_id, invoice(invoice_number="O", grand_total=Decimal("0.10")), final_status=constants.DECISION_APPROVED)
    finish(old, 10)
    # recent runs
    clock["t"] = now - timedelta(days=1)
    r1 = repo.create_processing_run(db, "r1.pdf", file_hash())
    repo.save_invoice(db, r1.run_id, invoice(invoice_number="A", grand_total=Decimal("0.20")), final_status=constants.DECISION_APPROVED)
    finish(r1, 20)
    clock["t"] = now - timedelta(days=2)
    r2 = repo.create_processing_run(db, "r2.pdf", file_hash())
    repo.save_invoice(
        db,
        r2.run_id,
        invoice(invoice_number="B", currency="EUR", grand_total=Decimal("1000.50")),
        final_status=constants.DECISION_REVIEW_REQUIRED,
    )
    finish(r2, 30)
    clock["t"] = now - timedelta(days=3)
    r3 = repo.create_processing_run(db, "r3.pdf", file_hash())
    repo.save_invoice(db, r3.run_id, None, final_status=constants.DECISION_PROCESSING_FAILED)
    finish(r3, 5, constants.RUN_FAILED, "boom")
    clock["t"] = now - timedelta(days=4)
    repo.create_processing_run(db, "running.pdf", file_hash())  # still running: no completed_at
    repo.create_purchase_order_if_missing(db, po(po_number="P1"))
    repo.create_purchase_order_if_missing(db, po(po_number="P2"))
    repo.create_purchase_order_if_missing(db, po(po_number="P3", status="CLOSED"))

    stats = repo.get_dashboard_stats(db, now=now)
    assert stats.total_runs == 5
    assert stats.runs_by_status == {"COMPLETED": 3, "FAILED": 1, "RUNNING": 1}
    assert stats.runs_last_7_days == 4
    assert stats.avg_run_duration_seconds == pytest.approx((10 + 20 + 30 + 5) / 4)
    assert stats.total_invoices == 4
    assert stats.invoices_by_status == {"APPROVED": 2, "REVIEW_REQUIRED": 1, "PROCESSING_FAILED": 1}
    assert stats.invoiced_total_by_currency == {"USD": Decimal("0.30"), "EUR": Decimal("1000.50")}
    assert stats.approved_total_by_currency == {"USD": Decimal("0.30")}
    assert all(isinstance(v, Decimal) for v in stats.invoiced_total_by_currency.values())
    assert (stats.open_purchase_orders, stats.closed_purchase_orders) == (2, 1)


def test_dashboard_stats_on_empty_database(db):
    stats = repo.get_dashboard_stats(db)
    assert stats.total_runs == 0 and stats.total_invoices == 0 and stats.avg_run_duration_seconds is None
    assert stats.invoiced_total_by_currency == {} and stats.open_purchase_orders == 0


# ============================================================================ persistence and transactions


def test_invoice_history_survives_application_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'history.db').as_posix()}"
    first = create_db_engine(url)
    init_db(first)
    with session_scope(first) as s:
        repo.create_purchase_order_if_missing(s, po())
        run = repo.create_processing_run(s, "inv.pdf", file_hash())
        repo.update_stage(s, run.run_id, "validate", constants.STAGE_COMPLETED, "ok")
        repo.save_invoice(s, run.run_id, invoice(), final_status=constants.DECISION_APPROVED, decision_reasons=["OK"])
        repo.complete_run(s, run.run_id, constants.RUN_COMPLETED)
        run_id = run.run_id
    first.dispose()

    second = create_db_engine(url)  # new process, same file
    init_db(second)
    with session_scope(second) as s:
        assert repo.get_run(s, run_id).run_status == "COMPLETED"
        assert [e.stage_name for e in repo.list_events(s, run_id)] == ["validate"]
        (inv,) = repo.search_invoices(s, invoice_number="INV-001")
        assert inv.grand_total == Decimal("107.00") and inv.decision_reasons == ["OK"]
        assert [i.id for i in repo.get_approved_invoices_for_po(s, "PO-1")] == [inv.id]
        assert repo.find_duplicate_invoices(s, vendor_name="acme industrial supplies", invoice_number="inv-001")
        assert repo.get_purchase_order(s, "PO-1") is not None
    second.dispose()


def test_failed_transaction_leaves_no_partial_run(db_engine):
    with pytest.raises(RuntimeError):
        with session_scope(db_engine) as s:
            run = repo.create_processing_run(s, "a.pdf", file_hash())
            repo.save_invoice(s, run.run_id, invoice(), final_status=constants.DECISION_REVIEW_REQUIRED)
            raise RuntimeError("crash mid-run")
    with session_scope(db_engine) as s:
        assert s.query(ProcessingRun).count() == 0 and s.query(Invoice).count() == 0
