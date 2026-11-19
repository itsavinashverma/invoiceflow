"""The UI's read layer, against data produced by the real pipeline (fake AI)."""

import csv
import io
from datetime import UTC, date, timedelta
from decimal import Decimal as D

import pytest
from fakes import FakeAiProvider, ground_truth

from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import ProcessingRun
from invoiceflow.database.seed import seed_purchase_orders
from invoiceflow.schemas import DecisionConfig, InvoiceExtraction
from invoiceflow.services import reporting
from invoiceflow.services.pipeline import InvoicePipeline


@pytest.fixture
def history(db_engine, settings, demo, gen_module):
    """Four real runs: approved, review (no PO), duplicate (same file again), failed (not a PDF)."""
    with session_scope(db_engine) as s:
        seed_purchase_orders(s)

    def run(name, payload, key):
        provider = FakeAiProvider(ground_truth(gen_module, name))
        return InvoicePipeline(settings, engine=db_engine, ai_provider=provider).process(payload, f"{key}.pdf")

    std, miss = demo["standard"].path.read_bytes(), demo["missing_po"].path.read_bytes()
    out = {
        "approved": run("invoice_standard_digital.pdf", std, "std"),
        "review": run("invoice_missing_po.pdf", miss, "missing"),
        "duplicate": run("invoice_standard_digital.pdf", std, "std_again"),
        "failed": InvoicePipeline(settings, engine=db_engine, ai_provider=FakeAiProvider(InvoiceExtraction(invoice_number="X"))).process(
            b"nope", "bad.pdf"
        ),
    }
    assert [r.decision_status for r in out.values()] == ["APPROVED", "REVIEW_REQUIRED", "DUPLICATE_BLOCKED", "PROCESSING_FAILED"]
    return db_engine, out


def test_search_runs_returns_every_run_newest_first(history):
    engine, out = history
    with session_scope(engine) as s:
        rows = reporting.search_runs(s)
    assert [r.filename for r in rows] == ["bad.pdf", "std_again.pdf", "missing.pdf", "std.pdf"]
    first = rows[-1]
    assert (first.decision_status, first.invoice_number, first.po_number, first.grand_total, first.currency) == (
        "APPROVED",
        "ACME-INV-20417",
        "PO-2024-1001",
        D("1301.12"),
        "USD",
    )
    assert first.duration_seconds > 0 and first.run_id == out["approved"].run_id


def test_search_filters(history):
    engine, _ = history
    with session_scope(engine) as s:
        names = lambda **kw: sorted(r.filename for r in reporting.search_runs(s, **kw))  # noqa: E731
        assert names(text="acme-inv-20433") == ["missing.pdf"]  # invoice number, case-insensitive
        assert names(text="PO-2024-1001") == ["std.pdf", "std_again.pdf"]
        assert names(text="acme industrial") == ["missing.pdf", "std.pdf", "std_again.pdf"]  # vendor
        # a file name; deliberately not "bad", which random hex run IDs sometimes contain ("a", "b", "d" are hex digits)
        assert names(text="bad.pdf") == ["bad.pdf"]
        assert names(statuses=["APPROVED"]) == ["std.pdf"]
        assert names(statuses=["REVIEW_REQUIRED", "DUPLICATE_BLOCKED"]) == ["missing.pdf", "std_again.pdf"]
        assert names(statuses=["PROCESSING_FAILED"]) == ["bad.pdf"]
        assert names(text="%") == []  # a wildcard is not "match everything"
        assert names(text="acme", statuses=["APPROVED"]) == ["std.pdf"]
        # Derive the bounds from the UTC timestamps created by this fixture. Local date.today()
        # can differ from the UTC calendar day (for example, in Asia/Kolkata just after midnight).
        run_days = [row.started_at.astimezone(UTC).date() for row in reporting.search_runs(s)]
        first_day, last_day = min(run_days), max(run_days)
        assert len(names(date_from=first_day - timedelta(days=1), date_to=last_day + timedelta(days=1))) == 4
        assert names(date_from=last_day + timedelta(days=1)) == []
        assert names(date_to=first_day - timedelta(days=1)) == []
        assert len(reporting.search_runs(s, limit=2)) == 2 and len(reporting.search_runs(s, limit=2, offset=3)) == 1


def test_date_filter_uses_inclusive_utc_days(db_engine):
    from datetime import datetime

    with session_scope(db_engine) as s:
        for i, when in enumerate(
            [
                datetime(2024, 5, 1, 0, 0, 1, tzinfo=UTC),
                datetime(2024, 5, 1, 23, 59, 59, tzinfo=UTC),
                datetime(2024, 5, 2, 0, 0, 0, tzinfo=UTC),
            ]
        ):
            s.add(
                ProcessingRun(
                    run_id=f"{i:032x}", original_filename=f"f{i}.pdf", file_hash=f"{i:064x}", run_status="RUNNING", started_at=when
                )
            )
    with session_scope(db_engine) as s:
        on_the_first = reporting.search_runs(s, date_from=date(2024, 5, 1), date_to=date(2024, 5, 1))
        assert sorted(r.filename for r in on_the_first) == ["f0.pdf", "f1.pdf"]
        assert on_the_first[0].decision_status is None  # a run with no invoice yet is "in progress"


def test_run_view_for_an_approved_run(history):
    engine, out = history
    with session_scope(engine) as s:
        v = reporting.get_run_view(s, out["approved"].run_id)
    assert v.run.decision_status == "APPROVED" and v.error is None
    assert v.invoice.invoice_number == "ACME-INV-20417" and len(v.invoice.line_items) == 3 and v.invoice.grand_total == D("1301.12")
    assert v.decision.matched_po_number == "PO-2024-1001" and v.decision.maximum_allowed_total == D("1240.32")
    assert [e.status for e in v.events].count("COMPLETED") == 11 and v.events[3].status == "SKIPPED"
    assert all(e.title for e in v.events) and v.ingestion["method"] == "text" and v.ai_record.model == "TEST-DOUBLE-NOT-GEMINI"


def test_run_view_for_failed_duplicate_and_review_runs(history):
    engine, out = history
    with session_scope(engine) as s:
        failed = reporting.get_run_view(s, out["failed"].run_id)
        dup = reporting.get_run_view(s, out["duplicate"].run_id)
        review = reporting.get_run_view(s, out["review"].run_id)
        assert reporting.get_run_view(s, "nope") is None
    assert failed.run.run_status == "FAILED" and failed.error.code == "bad_signature" and "Export or scan" in failed.error.action
    assert failed.error.stage == "upload_validation" and failed.invoice is None
    assert {"DUPLICATE_FILE", "DUPLICATE_INVOICE_NUMBER"} <= set(dup.decision.reason_codes) and dup.error is None
    assert review.decision.po_suggestions and review.decision.checks_failed[0].reason_code == "MISSING_PO_REFERENCE"


def test_po_overview_uses_only_approved_invoices(history):
    engine, _ = history
    with session_scope(engine) as s:
        rows = {p.po_number: p for p in reporting.po_overview(s, DecisionConfig())}
        acme = rows["PO-2024-1001"]
        assert len(rows) == 12
        # one approved invoice of 1,301.12 incl. 85.12 tax -> 1,216.00 net counted; the duplicate and review run are not
        assert (acme.total_amount, acme.approved_amount, acme.remaining, acme.approved_invoice_count) == (
            D("1216.00"),
            D("1216.00"),
            D("0.00"),
            1,
        )
        assert (acme.tolerance_percent, acme.allowed_overage, acme.maximum_allowed) == (D("2.00"), D("24.32"), D("24.32"))
        assert rows["PO-2024-1002"].approved_amount == D("0") and rows["PO-2024-1002"].remaining == rows["PO-2024-1002"].total_amount
        gross = {p.po_number: p for p in reporting.po_overview(s, DecisionConfig(po_amount_basis="grand_total"))}
        assert gross["PO-2024-1001"].approved_amount == D("1301.12")  # basis switches the counted amount
        assert [p.po_number for p in reporting.po_overview(s, DecisionConfig(), search="acme")] == ["PO-2024-1001"]
        assert [p.po_number for p in reporting.po_overview(s, DecisionConfig(), status="CLOSED")] == ["PO-2024-1007", "PO-2024-1009"]
        assert {p.currency for p in reporting.po_overview(s, DecisionConfig(), currency="EUR")} == {"EUR"}
        assert [r.invoice_number for r in reporting.po_approved_invoices(s, "po-2024-1001")] == ["ACME-INV-20417"]


def test_csv_report_is_exact_and_safe(history):
    engine, _ = history
    with session_scope(engine) as s:
        text = reporting.runs_to_csv(reporting.search_runs(s))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == reporting.CSV_COLUMNS and len(rows) == 4
    approved = next(r for r in rows if r["decision"] == "APPROVED")
    assert (approved["invoice_number"], approved["subtotal"], approved["tax_total"], approved["grand_total"], approved["currency"]) == (
        "ACME-INV-20417",
        "1216.00",
        "85.12",
        "1301.12",
        "USD",
    )
    failed = next(r for r in rows if r["decision"] == "PROCESSING_FAILED")
    assert failed["grand_total"] == "" and failed["decision_reasons"] == "PROCESSING_FAILED" and failed["run_status"] == "FAILED"


def test_csv_neutralises_spreadsheet_formulas_from_untrusted_documents():
    import datetime as dt

    row = reporting.RunRow(
        run_id="r" * 32,
        filename='=HYPERLINK("http://evil")',
        file_hash="a" * 64,
        started_at=dt.datetime(2024, 1, 1),
        run_status="COMPLETED",
        decision_status="APPROVED",
        invoice_number="+1+1",
        vendor_name="@SUM(A1)",
        po_number="-2",
        grand_total=D("-5.00"),
        currency="USD",
    )
    out = next(csv.DictReader(io.StringIO(reporting.runs_to_csv([row]))))
    assert (
        out["file_name"].startswith("'=")
        and out["invoice_number"].startswith("'+")
        and out["vendor"].startswith("'@")
        and out["po_number"].startswith("'-")
    )
    assert out["grand_total"] == "-5.00"  # real numbers are not mangled


def test_dashboard_stats_exclude_duplicates_and_failures_from_the_amount(history):
    engine, _ = history
    with session_scope(engine) as s:
        stats = repo.get_dashboard_stats(s)
    assert stats.total_runs == 4 and stats.total_invoices == 4
    assert stats.invoices_by_status == {"APPROVED": 1, "REVIEW_REQUIRED": 1, "DUPLICATE_BLOCKED": 1, "PROCESSING_FAILED": 1}
    assert stats.invoiced_total_by_currency == {"USD": D("1301.12") + D("463.31")}  # approved + review; the duplicate is not counted twice
    assert stats.approved_total_by_currency == {"USD": D("1301.12")}
