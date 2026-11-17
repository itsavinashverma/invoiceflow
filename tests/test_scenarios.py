"""Demonstration scenarios and technical cases, executed end to end.

Real synthetic PDFs, the real pipeline, real database, real OCR (when Tesseract is installed). The only
substitutes are test doubles for the AI provider; no test here calls Gemini. After every test that uses the
``world`` fixture, history-wide invariants are re-checked, so a scenario cannot pass while quietly leaving
the database in a state that could let something unsafe through.
"""

import dataclasses
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pymupdf
import pytest
from conftest import requires_tesseract
from fakes import FakeAiProvider, TextDrivenAiProvider, extraction_from_invoice, ground_truth
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from streamlit.testing.v1 import AppTest
from test_ai_extraction import FAKE_KEY, api_error, make_client, response
from ui_helpers import alltext, html, no_exceptions

from invoiceflow import constants
from invoiceflow.ai.provider import GeminiProvider
from invoiceflow.database import connection
from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import DatabaseUnavailableError, create_db_engine, session_scope
from invoiceflow.database.models import ExtractionRecordRow, Invoice, ProcessingEvent, ProcessingRun
from invoiceflow.extraction import pdf_extractor
from invoiceflow.schemas import DecisionConfig
from invoiceflow.services import pipeline as pipeline_module
from invoiceflow.services import reporting
from invoiceflow.services.maintenance import fail_abandoned_runs
from invoiceflow.services.pipeline import InvoicePipeline
from invoiceflow.services.validation_engine import DecisionBuilder

STAGES = [name for name, _ in constants.PIPELINE_STAGES]
APPROVED, REVIEW, DUPLICATE, FAILED = "APPROVED", "REVIEW_REQUIRED", "DUPLICATE_BLOCKED", "PROCESSING_FAILED"


# ============================================================================ the safety net around every scenario


def assert_history_consistent(engine) -> None:
    """Invariants that must hold for ALL stored history, whatever a test did."""
    with session_scope(engine) as s:
        for run in s.query(ProcessingRun).all():
            assert run.run_status != "RUNNING", f"run {run.run_id} was left hanging"
            events = {e.stage_name: e.stage_status for e in repo.list_events(s, run.run_id)}
            assert set(events) == set(STAGES), f"run {run.run_id} has an incomplete event list: {events}"
            assert not {"PENDING", "RUNNING"} & set(events.values()), f"unfinished stages in {run.run_id}: {events}"
            inv = s.query(Invoice).filter_by(processing_run_id=run.id).one_or_none()
            assert inv is not None, f"run {run.run_id} has no invoice record"
            if run.run_status == "FAILED":
                assert inv.final_status == FAILED and run.overall_error and "FAILED" in events.values()
            if inv.final_status == FAILED:
                assert run.run_status == "FAILED"
            if inv.final_status == APPROVED:  # the critical one: an approval requires a fully successful run
                assert run.run_status == "COMPLETED" and run.overall_error is None
                assert set(events.values()) <= {"COMPLETED", "SKIPPED"}, events
                assert inv.decision_reasons == [] and inv.decision_explanation["status"] == APPROVED
                assert inv.decision_explanation["checks_failed"] == []
                assert inv.invoice_number and inv.po_number and inv.grand_total is not None and inv.vendor_name
        for po in reporting.po_overview(s, DecisionConfig()):  # approved invoices may never overspend a PO
            assert po.approved_amount <= po.total_amount + po.allowed_overage, f"{po.po_number} overspent: {po}"
        approved_keys = [
            (i.normalized_vendor_name, (i.invoice_number or "").lower()) for i in s.query(Invoice).filter_by(final_status=APPROVED)
        ]
        assert len(approved_keys) == len(set(approved_keys)), "the same invoice was approved twice"


@pytest.fixture
def world(seeded):
    """A migrated database with the 12 demo POs. History invariants are verified when the test ends."""
    yield seeded
    assert_history_consistent(seeded)


# ============================================================================ helpers


def run_pipeline(world, settings, provider, data, name="invoice.pdf", **kw):
    return InvoicePipeline(settings, engine=world, ai_provider=provider).process(data, name, **kw)


def base_invoice(gen_module, file_name="invoice_standard_digital.pdf"):
    return next(i for i in gen_module.build_invoices(gen_module.build_purchase_orders()) if i.file_name == file_name)


def custom(gen_module, number, lines=None, **changes):
    """A variant of the standard Acme invoice (PO-2024-1001, PO total 1,216.00, tolerance 2%), rendered as a real PDF."""
    inv = dataclasses.replace(base_invoice(gen_module), invoice_number=number, **changes)
    if lines is not None:
        inv = dataclasses.replace(inv, lines=tuple(lines))
    return inv, gen_module.render_invoice_pdf(inv)


def one_line(gen_module, amount):
    return [gen_module.Line("AC-TEST", "Test supply", D("1"), D(amount))]


def process_custom(world, settings, gen_module, number, lines=None, **changes):
    inv, pdf = custom(gen_module, number, lines, **changes)
    return inv, run_pipeline(world, settings, FakeAiProvider(extraction_from_invoice(inv)), pdf, f"{number}.pdf")


def open_in_history(run_id) -> AppTest:
    at = AppTest.from_string("from invoiceflow.ui import history_page\nhistory_page.render()", default_timeout=60)
    at.session_state["history_open_run"] = run_id
    at.run()
    no_exceptions(at)
    return at


def stage_status(result):
    return {e.stage_name: e.status for e in result.stages}


def failed_checks(result):
    return {c.check: c.detail for c in result.decision.checks_failed}


# ============================================================================ SCENARIO 1: happy path


def test_scenario_1_happy_path_is_approved(world, settings, demo, gen_module):
    g = demo["standard"]
    r = run_pipeline(
        world, settings, FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")), g.path.read_bytes(), g.path.name
    )
    d = r.decision

    assert r.run_status == "COMPLETED" and r.decision_status == APPROVED and d.reasons == []
    passed = {c.check for c in d.checks_passed}
    for expected in (
        "invoice_number_present",  # correct invoice number
        "po_found",
        "po_reference_present",  # correct PO
        "vendor_matches_po",  # existing vendor
        "currency_matches_po",  # matching currency
        "po_is_open",
        "within_po_limit",  # amount within the remaining PO balance
        "line_arithmetic",
        "subtotal_matches_lines",
        "total_adds_up",  # valid financial arithmetic
        "not_duplicate_file",
        "not_duplicate_invoice_number",
    ):
        assert expected in passed, f"{expected} did not pass: {[c.check for c in d.checks_failed]}"
    assert d.checks_failed == []
    assert d.invoice_amount <= d.maximum_allowed_total and d.remaining_balance == D("1216.00")
    assert stage_status(r) == {**dict.fromkeys(STAGES, "COMPLETED"), "ocr": "SKIPPED"}
    with session_scope(world) as s:
        po = next(p for p in reporting.po_overview(s, DecisionConfig()) if p.po_number == "PO-2024-1001")
    assert (po.approved_amount, po.remaining, po.approved_invoice_count) == (D("1216.00"), D("0.00"), 1)  # the PO balance moved


# ============================================================================ SCENARIO 2: scanned invoice (real OCR)


@requires_tesseract
def test_scenario_2_scanned_invoice_goes_through_real_ocr(world, settings, demo, gen_module):
    g = demo["scanned"]
    provider = TextDrivenAiProvider()  # can only extract what is literally in the text it is given
    r = run_pipeline(world, settings, provider, g.path.read_bytes(), g.path.name)

    # 1. normal PDF extraction is insufficient
    page = r.ingestion.pages[0]
    assert page.text_layer_chars == 0 and page.has_images and page.ocr_attempted
    text_stage = next(e for e in r.stages if e.stage_name == "text_extraction")
    assert text_stage.status == "COMPLETED" and "no usable text layer" in text_stage.message
    # 2. OCR runs
    assert stage_status(r)["ocr"] == "COMPLETED" and r.ocr.needed and r.ocr.performed and r.ocr.text_method == "ocr"
    assert r.ocr.confidences[1] is not None and r.ocr.confidences[1] > 0
    assert provider.calls[0]["source_method"] == "ocr"
    # 3. the relevant fields really come out of the OCR text
    ocr_text = r.ingestion.text
    for token in (g.invoice_number.split("-")[-1], g.po_number, f"{g.subtotal:,.2f}", f"{g.total:,.2f}"):
        assert token in ocr_text, f"OCR output lacks {token!r}"
    inv = r.invoice
    assert (inv.invoice_number, inv.purchase_order_number, inv.vendor_name) == (g.invoice_number, g.po_number, g.vendor_name)
    assert (inv.subtotal, inv.grand_total, inv.currency) == (g.subtotal, g.total, "USD") and len(inv.line_items) >= 2
    # 4. processing continued through every remaining stage
    assert all(stage_status(r)[s] == "COMPLETED" for s in STAGES)
    # 5. correct, or explicitly sent for review when OCR is uncertain: never silently wrong
    if r.decision_status == APPROVED:
        assert (inv.subtotal, inv.grand_total) == (g.subtotal, g.total)
    else:
        assert r.decision_status == REVIEW and r.decision.reasons and r.decision.human_actions
    assert r.decision_status in (APPROVED, REVIEW)


@requires_tesseract
@pytest.mark.parametrize("dpi", [60, 35])
def test_scenario_2b_a_poor_scan_is_never_approved(world, settings, demo, gen_module, dpi):
    inv = base_invoice(gen_module, "invoice_scanned.pdf")
    poor = gen_module.render_scanned_pdf(gen_module.render_invoice_pdf(inv), dpi=dpi)
    r = run_pipeline(world, settings, TextDrivenAiProvider(), poor, "poor_scan.pdf")
    assert r.decision_status in (REVIEW, FAILED) and r.decision_status != APPROVED  # explicit, not silent
    assert r.ocr.performed and r.ocr.confidences[1] < constants.OCR_LOW_CONFIDENCE
    assert any("low OCR confidence" in w for w in r.ocr.warnings)  # the doubt is recorded
    if r.decision_status == REVIEW:
        assert "EXTRACTION_UNCERTAIN" in r.decision.reason_codes
    else:
        assert r.error.manual_entry_possible  # nothing could be read: the user is told what to do


def test_scenario_2c_without_tesseract_the_scan_stops_with_an_actionable_error(world, settings, demo, gen_module):
    no_ocr = replace(settings, tesseract_cmd=r"C:\definitely\missing\tesseract.exe")
    provider = TextDrivenAiProvider()
    r = run_pipeline(world, no_ocr, provider, demo["scanned"].path.read_bytes(), "scan.pdf")
    assert r.run_status == "FAILED" and r.error.code == "ocr_unavailable" and "TESSERACT_CMD" in r.error.message
    assert provider.calls == [] and r.decision_status == FAILED


# ============================================================================ SCENARIO 3: duplicate invoice


def test_scenario_3_duplicate_names_the_earlier_invoice_that_caused_it(world, settings, demo, gen_module):
    data = demo["standard"].path.read_bytes()
    provider = FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf"))
    first = run_pipeline(world, settings, provider, data, "original.pdf")
    second = run_pipeline(world, settings, provider, data, "resubmitted.pdf")

    assert (first.decision_status, second.decision_status) == (APPROVED, DUPLICATE)
    assert {"DUPLICATE_FILE", "DUPLICATE_INVOICE_NUMBER"} <= set(second.decision.reason_codes)
    by_number = failed_checks(second)["not_duplicate_invoice_number"]
    # it says WHICH earlier invoice: its number, amount, status and run
    assert "ACME-INV-20417" in by_number and "$1,301.12" in by_number and "approved" in by_number
    assert f"run {first.run_id[:8]}" in by_number and "Acme Industrial Supplies Ltd" in by_number
    assert f"run {first.run_id[:8]}" in failed_checks(second)["not_duplicate_file"]
    assert second.decision.summary.startswith("Blocked as a duplicate")

    # and the same explanation is what a person sees in the application
    at = open_in_history(second.run_id)
    shown = html(at)
    assert "Duplicate blocked" in shown and f"run {first.run_id[:8]}" in shown and "ACME-INV-20417" in shown
    # the duplicate did not touch the PO balance
    with session_scope(world) as s:
        assert [i.invoice_number for i in repo.get_approved_invoices_for_po(s, "PO-2024-1001")] == ["ACME-INV-20417"]


def test_scenario_3b_same_vendor_and_number_in_a_different_file_is_still_blocked(world, settings, demo, gen_module):
    first_inv, first_pdf = custom(gen_module, "DUP-7", one_line(gen_module, "300.00"))
    first = run_pipeline(world, settings, FakeAiProvider(extraction_from_invoice(first_inv)), first_pdf, "first.pdf")
    # a re-keyed copy: different bytes, different amount, same vendor and invoice number (spacing/case differ too)
    resent_inv, resent_pdf = custom(gen_module, "dup-7", one_line(gen_module, "310.00"), notes="Corrected copy.")
    assert resent_pdf != first_pdf
    resent = run_pipeline(world, settings, FakeAiProvider(extraction_from_invoice(resent_inv)), resent_pdf, "second.pdf")
    assert first.decision_status == APPROVED and resent.decision_status == DUPLICATE
    assert resent.decision.reason_codes.count("DUPLICATE_INVOICE_NUMBER") == 1 and "DUPLICATE_FILE" not in resent.decision.reason_codes
    assert f"run {first.run_id[:8]}" in failed_checks(resent)["not_duplicate_invoice_number"]


def test_scenario_3c_two_different_invoices_with_the_same_amount_are_not_duplicates(world, settings, gen_module):
    a = process_custom(world, settings, gen_module, "SAME-AMOUNT-1", one_line(gen_module, "400.00"))[1]
    b = process_custom(world, settings, gen_module, "SAME-AMOUNT-2", one_line(gen_module, "400.00"))[1]
    assert (a.decision_status, b.decision_status) == (APPROVED, APPROVED)
    assert a.invoice.grand_total == b.invoice.grand_total and a.file_hash != b.file_hash


def test_scenario_3d_five_repeated_uploads_approve_once(world, settings, demo, gen_module):
    data = demo["standard"].path.read_bytes()
    provider = FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf"))
    results = [run_pipeline(world, settings, provider, data, f"try{i}.pdf") for i in range(5)]
    assert [r.decision_status for r in results] == [APPROVED] + [DUPLICATE] * 4
    with session_scope(world) as s:
        assert s.query(ProcessingRun).count() == 5 and len(repo.get_approved_invoices_for_po(s, "PO-2024-1001")) == 1


def test_scenario_3e_simultaneous_uploads_of_the_same_invoice(world, settings, demo, gen_module):
    data = demo["standard"].path.read_bytes()
    results, errors, barrier = [None] * 4, [], threading.Barrier(4)

    def worker(i):
        try:
            pipe = InvoicePipeline(
                settings, engine=world, ai_provider=FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf"))
            )
            barrier.wait(timeout=15)
            results[i] = pipe.process(data, f"c{i}.pdf")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]
    assert not errors, errors
    assert sorted(r.decision_status for r in results) == [APPROVED, DUPLICATE, DUPLICATE, DUPLICATE]


# ============================================================================ SCENARIO 4: PO amount exceeded


def test_scenario_4_amount_over_the_po_limit_shows_the_discrepancy_and_tolerance(world, settings, gen_module):
    base = base_invoice(gen_module)
    lines = [dataclasses.replace(base.lines[0], quantity=D("20")), *base.lines[1:]]  # 20 hoses instead of 12
    inv, r = process_custom(world, settings, gen_module, "OVER-1", lines)

    # expected figures, derived independently from the generator data and the seeded PO (1,216.00, tolerance 2%)
    po_total, tolerance = D("1216.00"), D("2.00")
    allowed = (po_total * tolerance / 100).quantize(D("0.01"))
    maximum = po_total + allowed
    excess = inv.subtotal - maximum
    assert (allowed, maximum, excess) == (D("24.32"), D("1240.32"), D("339.68"))

    assert r.run_status == "COMPLETED" and r.decision_status == REVIEW and r.decision.reason_codes == ["AMOUNT_EXCEEDS_PO_LIMIT"]
    msg = failed_checks(r)["within_po_limit"]
    for fragment in (f"${inv.subtotal:,.2f}", f"${maximum:,.2f}", f"${excess:,.2f}", "2% tolerance", f"${allowed:,.2f}", "$1,216.00"):
        assert fragment in msg, f"{fragment!r} missing from: {msg}"
    d = r.decision
    assert (d.po_amount, d.tolerance_percent, d.allowed_overage, d.maximum_allowed_total, d.invoice_amount) == (
        po_total,
        tolerance,
        allowed,
        maximum,
        inv.subtotal,
    )
    assert any("Ask the PO owner to approve the extra amount" in a for a in d.human_actions)

    shown = html(open_in_history(r.run_id))  # and the reviewer sees it
    assert "Review required" in shown and f"${excess:,.2f}" in shown and "2% tolerance" in shown
    with session_scope(world) as s:
        assert repo.get_approved_invoices_for_po(s, "PO-2024-1001") == []  # nothing counted against the PO


@pytest.mark.parametrize("amount, status", [("1240.32", APPROVED), ("1240.33", REVIEW), ("1216.00", APPROVED), ("2000.00", REVIEW)])
def test_scenario_4b_the_limit_is_exact_to_the_cent(world, settings, gen_module, amount, status):
    _, r = process_custom(world, settings, gen_module, f"EDGE-{amount}", one_line(gen_module, amount))
    assert r.decision_status == status, r.decision.summary


def test_scenario_4c_partial_invoices_share_one_po_and_the_last_one_is_stopped(world, settings, gen_module):
    results = [
        process_custom(world, settings, gen_module, f"PART-{n}", one_line(gen_module, amt))[1]
        for n, amt in ((1, "400.00"), (2, "500.00"), (3, "340.32"), (4, "0.01"))
    ]
    # 400 + 500 + 340.32 = 1,240.32 = PO total + 2% tolerance exactly; one more cent is over
    assert [r.decision_status for r in results] == [APPROVED, APPROVED, APPROVED, REVIEW]
    assert results[3].decision.already_approved_amount == D("1240.32") and results[3].decision.reason_codes == ["AMOUNT_EXCEEDS_PO_LIMIT"]


def test_scenario_4d_arithmetic_mismatch_is_caught(world, settings, gen_module):
    inv, pdf = custom(gen_module, "ARITH-1", one_line(gen_module, "500.00"))
    wrong = extraction_from_invoice(inv, grand_total="999.99")  # the "extraction" disagrees with its own subtotal + tax
    r = run_pipeline(world, settings, FakeAiProvider(wrong), pdf, "arith.pdf")
    assert r.decision_status == REVIEW and "TOTAL_MISMATCH" in r.decision.reason_codes


# ============================================================================ SCENARIO 5: missing PO reference


def test_scenario_5_missing_po_suggests_candidates_without_inferring_a_reference(world, settings, gen_module):
    # a numerically perfect invoice for Acme's PO, but it prints no PO number
    inv, r = process_custom(world, settings, gen_module, "NOPO-1", po_number=None)
    assert r.run_status == "COMPLETED" and r.decision_status == REVIEW and r.decision.reason_codes == ["MISSING_PO_REFERENCE"]
    assert r.decision.matched_po_number is None and r.invoice.purchase_order_number is None  # nothing was inferred
    assert [s.po_number for s in r.po_matching.suggestions] == ["PO-2024-1001"] and r.po_matching.suggestions[0].status == "OPEN"
    assert any("Suggestion only" in n and "NOT been applied" in n for n in r.decision.notes)
    assert "within_po_limit" in {c.check for c in r.decision.checks_skipped}  # no balance was computed against a guessed PO

    with session_scope(world) as s:
        stored = s.query(Invoice).one()
        assert stored.po_number is None and stored.final_status == REVIEW  # the invoice record itself is unchanged
        assert repo.get_approved_invoices_for_po(s, "PO-2024-1001") == []
    shown = html(open_in_history(r.run_id))
    assert "Candidate purchase orders" in shown and "never applied automatically" in shown and "What a reviewer must verify" in shown
    assert any(
        d.value["PO"].tolist() == ["PO-2024-1001"]
        for d in open_in_history(r.run_id).dataframe
        if "PO" in d.value.columns and "Remaining" in d.value.columns
    )


def test_scenario_5b_several_possible_pos_are_ambiguous(world, settings, gen_module):
    from datetime import date

    from invoiceflow.database.seed import load_purchase_orders  # noqa: F401  (documenting the source of the first PO)
    from invoiceflow.schemas import PurchaseOrderData

    with session_scope(world) as s:
        repo.create_purchase_order_if_missing(
            s,
            PurchaseOrderData(
                po_number="PO-2024-2001",
                vendor_name="Acme Industrial Supplies Limited",
                currency="USD",
                total_amount=D("2000.00"),
                tolerance_percent=D("2.00"),
                po_date=date(2024, 10, 1),
            ),
        )
    _, r = process_custom(world, settings, gen_module, "NOPO-2", po_number=None)
    assert r.decision_status == REVIEW and {"MISSING_PO_REFERENCE", "AMBIGUOUS_PO_MATCH"} <= set(r.decision.reason_codes)
    assert {s.po_number for s in r.po_matching.suggestions} == {"PO-2024-1001", "PO-2024-2001"} and r.decision.matched_po_number is None


# ============================================================================ technical cases: documents


def test_corrupt_pdf(world, settings):
    r = run_pipeline(
        world, settings, FakeAiProvider(extraction_from_invoice_stub()), b"%PDF-1.7\n" + b"\x00\xff corrupt " * 60, "corrupt.pdf"
    )
    assert r.run_status == "FAILED" and r.error.stage == "upload_validation" and r.error.code == "corrupt_pdf"
    assert r.decision_status == FAILED and stage_status(r)["ai_extraction"] == "SKIPPED"


def test_truncated_pdf_is_never_approved(world, settings, demo, gen_module):
    whole = demo["standard"].path.read_bytes()
    r = run_pipeline(
        world, settings, FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")), whole[: len(whole) // 2], "cut.pdf"
    )
    assert r.decision_status != APPROVED  # either rejected as corrupt, or repaired and flagged for review


def test_empty_files(world, settings):
    provider = FakeAiProvider(extraction_from_invoice_stub())
    zero = run_pipeline(world, settings, provider, b"", "zero.pdf")
    assert zero.error.code == "empty_file" and zero.decision_status == FAILED
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        blank = doc.tobytes()
    r = run_pipeline(world, settings, provider, blank, "blank.pdf")  # a valid PDF with nothing on its pages
    assert r.error.stage == "text_extraction" and r.error.code == "no_text" and r.decision_status == FAILED
    assert provider.calls == []


def test_multipage_invoice_is_read_fully(world, settings, demo, gen_module):
    g = demo["multipage"]
    r = run_pipeline(world, settings, FakeAiProvider(ground_truth(gen_module, "invoice_multipage.pdf")), g.path.read_bytes(), g.path.name)
    assert r.ingestion.page_count >= 2 and r.decision_status == APPROVED
    assert f"{g.total:,.2f}" not in r.ingestion.pages[0].text and f"{g.total:,.2f}" in r.ingestion.pages[-1].text  # totals on the last page
    assert len(r.invoice.line_items) == 43 and r.invoice.grand_total == g.total


# ============================================================================ technical cases: extracted values


def test_missing_invoice_number(world, settings, demo, gen_module):
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf", invoice_number=None)),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    assert r.decision_status == REVIEW and r.decision.reason_codes == ["MISSING_INVOICE_NUMBER"]
    assert "invoice_number" in r.invoice.missing_fields


def test_missing_vendor(world, settings, demo, gen_module):
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf", vendor_name=None)),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    assert r.decision_status == REVIEW and "MISSING_VENDOR" in r.decision.reason_codes


@pytest.mark.parametrize("bad_date", ["31/02/2024", "not a date", "2024-13-45", "03/04/2024"])
def test_invalid_or_ambiguous_dates_are_flagged_for_review(world, settings, demo, gen_module, bad_date):
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf", invoice_date=bad_date)),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    assert r.invoice.invoice_date is None  # never guessed
    assert r.decision_status == REVIEW and "EXTRACTION_UNCERTAIN" in r.decision.reason_codes
    assert any("invoice_date" in w for w in r.ai.warnings)


def test_currency_mismatch(world, settings, demo, gen_module):
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf", currency="EUR")),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    assert (
        r.decision_status == REVIEW
        and r.decision.reason_codes == ["CURRENCY_MISMATCH"]
        and "EUR" in failed_checks(r)["currency_matches_po"]
    )


def test_vendor_mismatch(world, settings, demo, gen_module):
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf", vendor_name="Globex IT Services LLC")),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    assert r.decision_status == REVIEW and r.decision.reason_codes == ["VENDOR_MISMATCH"]
    assert "Globex" in failed_checks(r)["vendor_matches_po"] and "Acme Industrial Supplies Ltd" in failed_checks(r)["vendor_matches_po"]


def test_closed_po(world, settings, demo, gen_module):
    g = next(x for x in demo["all"] if x.path.name == "invoice_wayne_closed_po.pdf")
    r = run_pipeline(world, settings, FakeAiProvider(ground_truth(gen_module, g.path.name)), g.path.read_bytes(), g.path.name)
    assert r.decision_status == REVIEW and r.decision.reason_codes == ["PO_CLOSED"] and "closed" in failed_checks(r)["po_is_open"]


# ============================================================================ technical cases: Gemini failures (real client, fake SDK)


def gemini_pipeline(world, settings, *outcomes):
    client, sdk, sleeps = make_client(*outcomes)
    keyed = replace(settings, gemini_api_key=FAKE_KEY, gemini_model="fake-model")
    return InvoicePipeline(keyed, engine=world, ai_provider=GeminiProvider(keyed, client=client)), sdk, sleeps


def test_invalid_gemini_json_fails_the_run_after_bounded_retries(world, settings, demo):
    pipe, sdk, sleeps = gemini_pipeline(world, settings, *[response("{ this is not json")] * 3)
    r = pipe.process(demo["standard"].path.read_bytes(), "a.pdf")
    assert r.run_status == "FAILED" and r.error.stage == "ai_extraction" and r.error.code == "invalid_response"
    assert len(sdk.models.calls) == 3 and len(sleeps) == 2 and r.ai.attempts == 3  # bounded, not endless
    assert r.error.manual_entry_possible and r.decision_status == FAILED and r.ingestion is not None  # the text is kept
    with session_scope(world) as s:
        rec = s.query(ExtractionRecordRow).one()
        assert rec.status == "failed" and rec.raw_response == "{ this is not json"  # the bad answer is kept for audit


def test_gemini_answer_that_breaks_the_schema_is_rejected(world, settings, demo):
    bad = json.dumps({"grand_total": 1301.12, "line_items": "nope"})  # a float and a wrong type
    pipe, sdk, _ = gemini_pipeline(world, settings, *[response(bad)] * 3)
    r = pipe.process(demo["standard"].path.read_bytes(), "a.pdf")
    assert r.error.code == "invalid_response" and "did not match the invoice schema" in r.error.message and r.decision_status == FAILED


def test_gemini_timeout(world, settings, demo, gen_module):
    class ReadTimeout(Exception):
        pass

    pipe, sdk, _ = gemini_pipeline(world, settings, *[ReadTimeout("slow")] * 3)
    r = pipe.process(demo["standard"].path.read_bytes(), "a.pdf")
    assert r.error.code == "service_unavailable" and "ReadTimeout" in r.error.message and len(sdk.models.calls) == 3
    assert r.run_status == "FAILED" and r.decision_status == FAILED

    good = ground_truth(gen_module, "invoice_standard_digital.pdf").model_dump_json()
    pipe, sdk, sleeps = gemini_pipeline(world, settings, ReadTimeout("slow"), response(good))  # one timeout, then it works
    r = pipe.process(demo["standard"].path.read_bytes(), "b.pdf")
    assert r.decision_status == APPROVED and r.ai.attempts == 2 and len(sleeps) == 1


def test_gemini_rate_limit(world, settings, demo, gen_module):
    pipe, sdk, sleeps = gemini_pipeline(world, settings, api_error(429, "quota", retry_delay="120s"))
    r = pipe.process(demo["standard"].path.read_bytes(), "a.pdf")
    assert r.error.code == "rate_limited" and len(sdk.models.calls) == 1 and sleeps == []  # a long wait fails fast
    assert "Wait a minute" in r.error.action and r.decision_status == FAILED

    good = ground_truth(gen_module, "invoice_standard_digital.pdf").model_dump_json()
    pipe, sdk, sleeps = gemini_pipeline(world, settings, api_error(429, "quota", retry_delay="2s"), response(good))
    r = pipe.process(demo["standard"].path.read_bytes(), "b.pdf")
    assert r.decision_status == APPROVED and sleeps == [2.0]  # a short suggested delay is honoured, then it succeeds


def test_missing_api_key_uses_the_real_provider_and_fails_honestly(world, settings, demo):
    pipe = InvoicePipeline(replace(settings, gemini_api_key=None), engine=world)  # default provider: Gemini, no key
    assert isinstance(pipe.provider, GeminiProvider) and pipe.provider.is_test_double is False
    r = pipe.process(demo["standard"].path.read_bytes(), "a.pdf")
    assert r.error.code == "config_error" and "GEMINI_API_KEY" in r.error.message and r.decision_status == FAILED
    assert r.ai.provider == "gemini" and r.error.manual_entry_possible


def test_digital_invoices_do_not_need_tesseract(world, settings, demo, gen_module):
    no_ocr = replace(settings, tesseract_cmd=r"C:\definitely\missing\tesseract.exe")
    r = run_pipeline(
        world, no_ocr, FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")), demo["standard"].path.read_bytes(), "a.pdf"
    )
    assert r.decision_status == APPROVED and stage_status(r)["ocr"] == "SKIPPED"


# ============================================================================ technical cases: database failures


def test_database_unreachable_when_a_run_starts(settings, demo, monkeypatch):
    monkeypatch.setattr(connection, "CONNECT_TIMEOUT_SECONDS", 1)
    dead = create_db_engine("postgresql+psycopg://svc:Sup3rSecret@127.0.0.1:1/appdb")
    pipe = InvoicePipeline(settings, engine=dead, ai_provider=FakeAiProvider(extraction_from_invoice_stub()))
    with pytest.raises(DatabaseUnavailableError) as exc:
        pipe.process(demo["standard"].path.read_bytes(), "a.pdf")
    assert "Sup3rSecret" not in str(exc.value) and "svc" not in str(exc.value) and "not usable" in str(exc.value)
    dead.dispose()


def test_the_ui_reports_a_database_failure_during_processing_without_crashing(world, settings, demo, monkeypatch):
    monkeypatch.setattr(connection, "CONNECT_TIMEOUT_SECONDS", 1)
    dead = create_db_engine("postgresql+psycopg://svc:Sup3rSecret@127.0.0.1:1/appdb")
    from invoiceflow.ui import context

    monkeypatch.setattr(
        context, "make_pipeline", lambda s: InvoicePipeline(s, engine=dead, ai_provider=FakeAiProvider(extraction_from_invoice_stub()))
    )
    at = AppTest.from_string("from invoiceflow.ui import process_page\nprocess_page.render()", default_timeout=60).run()
    at.file_uploader(key="uploader_0").upload("a.pdf", demo["standard"].path.read_bytes(), "application/pdf").run()
    at.button(key="process_btn").click().run()
    no_exceptions(at)
    assert "could not be completed or recorded" in alltext(at) and "Sup3rSecret" not in alltext(at)
    dead.dispose()


def inject_unexpected_db_failure(monkeypatch, engine):
    """The final transaction's COMMIT fails (after every stage reported success inside it)."""
    state = {"armed": False}

    @event.listens_for(engine, "commit")
    def explode(conn):
        if state["armed"]:
            state["armed"] = False
            raise OperationalError("COMMIT", {}, Exception("simulated disk failure"))

    return state


def test_commit_failure_never_leaves_an_approval_or_a_false_completed_stage(world, settings, demo, gen_module):
    state = inject_unexpected_db_failure(None, world)

    def arm_on_last_stage(ev):
        if ev.stage_name == "save_result" and ev.status == "COMPLETED":
            state["armed"] = True  # the next commit (the decision transaction's) will fail

    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")),
        demo["standard"].path.read_bytes(),
        "a.pdf",
        on_event=arm_on_last_stage,
    )
    assert r.run_status == "FAILED" and r.decision_status == FAILED
    assert r.error.stage == "save_result" and "database rejected the change" in r.error.message
    assert stage_status(r)["save_result"] == "FAILED"  # corrected: it had been marked COMPLETED before the commit failed
    with session_scope(world) as s:
        assert s.query(Invoice).one().final_status == FAILED
        assert repo.get_approved_invoices_for_po(s, "PO-2024-1001") == []


def test_database_dying_mid_run_is_reported_not_raised(world, settings, demo, gen_module, monkeypatch):
    real = repo.update_stage
    calls = {"n": 0}

    def dying(session, run_id, stage, status, *a, **k):
        calls["n"] += 1
        if calls["n"] > 16:  # after the plan and a few stages, every write fails (the database "went away")
            raise OperationalError("UPDATE", {}, Exception("connection lost"))
        return real(session, run_id, stage, status, *a, **k)

    monkeypatch.setattr(repo, "update_stage", dying)
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    monkeypatch.setattr(repo, "update_stage", real)
    # the failure was reported to the caller, not raised, and the result says honestly that it could not be saved
    assert r.run_status == "FAILED" and r.error is not None and r.decision_status != APPROVED
    assert r.persistence_error and "could not be saved" in r.persistence_error
    with session_scope(world) as s:
        assert s.query(Invoice).filter_by(final_status=APPROVED).count() == 0
        assert repo.get_approved_invoices_for_po(s, "PO-2024-1001") == []
        orphan = repo.get_run(s, r.run_id)
        assert orphan.run_status == "RUNNING"  # nothing could be written while the database was gone...
    # ...so the next start-up closes it honestly instead of leaving it "in progress" forever
    with session_scope(world) as s:
        assert fail_abandoned_runs(s, older_than=timedelta(0)) == 1
    with session_scope(world) as s:
        closed = repo.get_run(s, r.run_id)
        assert closed.run_status == "FAILED" and "interrupted" in closed.overall_error
        assert s.query(Invoice).filter_by(processing_run_id=closed.id).one().final_status == FAILED
        assert all(e.stage_status in ("COMPLETED", "FAILED", "SKIPPED") for e in repo.list_events(s, r.run_id))


# Fault injection at EVERY stage: whatever goes wrong, nothing is approved and the failure is recorded honestly.
FAULTS = [
    ("upload_validation", "invoiceflow.services.pipeline.validate_pdf_upload", "standard"),
    ("text_extraction", "invoiceflow.extraction.pdf_extractor.read_text_layer", "standard"),
    ("ocr", "invoiceflow.extraction.pdf_extractor.run_ocr", "scanned"),
    ("ai_extraction", None, "standard"),
    ("structure_validation", "invoiceflow.services.pipeline.InvoiceExtraction", "standard"),
    ("normalization", "invoiceflow.services.pipeline.build_record", "standard"),
    ("duplicate_check", DecisionBuilder.duplicate_checks, "standard"),
    ("po_matching", DecisionBuilder.po_checks, "standard"),
    ("financial_validation", DecisionBuilder.financial_checks, "standard"),
    ("decision", DecisionBuilder.finish, "standard"),
    ("save_result", "invoiceflow.database.repositories.save_invoice", "standard"),
]


@pytest.mark.parametrize("stage, target, doc", FAULTS, ids=[f[0] for f in FAULTS])
def test_a_crash_in_any_stage_is_recorded_and_never_approved(world, settings, demo, gen_module, monkeypatch, stage, target, doc):
    class Boom(RuntimeError):
        pass

    def boom(*a, **k):
        raise Boom("injected fault")

    provider = FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf"))
    if stage == "ai_extraction":
        provider = FakeAiProvider(crash=Boom("injected fault"))
    elif stage == "structure_validation":
        monkeypatch.setattr(target, type("Stub", (), {"model_validate": staticmethod(boom)}), raising=True)
    elif stage == "save_result":
        real = repo.save_invoice
        monkeypatch.setattr(
            repo,
            "save_invoice",
            lambda s, rid, n, *, final_status, **kw: boom() if final_status != FAILED else real(s, rid, n, final_status=final_status, **kw),
        )
    elif isinstance(target, str):
        monkeypatch.setattr(target, boom)
    else:
        monkeypatch.setattr(DecisionBuilder, target.__name__, boom)

    r = run_pipeline(world, settings, provider, demo[doc].path.read_bytes(), "a.pdf")
    st = stage_status(r)
    assert r.run_status == "FAILED" and r.decision_status == FAILED and r.error.code == "internal_error" and r.error.stage == stage
    assert st[stage] == "FAILED"
    order = STAGES.index(stage)
    assert all(st[s] in ("COMPLETED", "SKIPPED") for s in STAGES[:order])  # earlier work is intact
    assert all(st[s] == "SKIPPED" for s in STAGES[order + 1 :] if s != "save_result")
    with session_scope(world) as s:
        stored = {e.stage_name: e.stage_status for e in repo.list_events(s, r.run_id)}
        assert stored == st, "the stored history differs from what the user was shown"
        assert s.query(Invoice).filter_by(final_status=APPROVED).count() == 0
        assert s.query(Invoice).one().final_status == FAILED
        assert repo.get_run(s, r.run_id).run_status == "FAILED"


# ============================================================================ technical cases: reruns and repeated attempts


def snapshot(engine):
    with session_scope(engine) as s:
        return (
            sorted((r.run_id, r.run_status, r.completed_at) for r in s.query(ProcessingRun)),
            sorted((i.processing_run_id, i.final_status, i.grand_total, tuple(i.decision_reasons)) for i in s.query(Invoice)),
            sorted((e.processing_run_id, e.stage_name, e.stage_status, e.duration_ms) for e in s.query(ProcessingEvent)),
            s.query(ExtractionRecordRow).count(),
        )


def test_streamlit_reruns_never_change_stored_history(world, settings, demo, gen_module, monkeypatch):
    from invoiceflow.ui import context

    provider = FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf"))
    monkeypatch.setattr(context, "make_pipeline", lambda s: InvoicePipeline(s, ai_provider=provider))
    at = AppTest.from_string("from invoiceflow.ui import process_page\nprocess_page.render()", default_timeout=60).run()
    at.file_uploader(key="uploader_0").upload("a.pdf", demo["standard"].path.read_bytes(), "application/pdf").run()
    at.button(key="process_btn").click().run()
    after_run = snapshot(world)
    assert len(after_run[0]) == 1

    for _ in range(6):  # the same page, rerun again and again
        at.run()
        no_exceptions(at)
    assert snapshot(world) == after_run and len(provider.calls) == 1

    for module in ("dashboard", "history_page", "orders_page", "health_page"):  # and every read-only page, repeatedly
        view = AppTest.from_string(f"from invoiceflow.ui import {module}\n{module}.render()", default_timeout=60)
        for _ in range(3):
            view.run()
            no_exceptions(view)
    assert snapshot(world) == after_run  # browsing the application writes nothing


def test_a_failed_run_never_changes_earlier_history(world, settings, demo, gen_module):
    ok = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")),
        demo["standard"].path.read_bytes(),
        "ok.pdf",
    )
    before = snapshot(world)
    for fault in (b"garbage", b"", b"%PDF-1.4 broken"):
        run_pipeline(world, settings, FakeAiProvider(extraction_from_invoice_stub()), fault, "bad.pdf")
    after = snapshot(world)
    assert [x for x in after[0] if x[0] == ok.run_id] == [x for x in before[0] if x[0] == ok.run_id]  # the good run is untouched
    assert set(before[2]) <= set(after[2]) and set(before[1]) <= set(after[1])  # nothing earlier was altered or lost
    assert len(after[0]) == 4


def test_abandoned_runs_are_closed_honestly_and_live_ones_are_left_alone(world, settings, demo, gen_module):
    ok = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")),
        demo["standard"].path.read_bytes(),
        "ok.pdf",
    )
    with session_scope(world) as s:  # a run whose process was killed half-way, and one that is genuinely in progress
        stale = repo.create_processing_run(s, "killed.pdf", "a" * 64)
        stale.started_at = datetime.now(UTC) - timedelta(hours=2)
        live = repo.create_processing_run(s, "running.pdf", "b" * 64)
        for run in (stale, live):
            for i, (name, _) in enumerate(constants.PIPELINE_STAGES):
                repo.update_stage(s, run.run_id, name, "COMPLETED" if i < 2 else ("RUNNING" if i == 2 else "PENDING"))
        stale_id, live_id = stale.run_id, live.run_id
    with session_scope(world) as s:
        assert fail_abandoned_runs(s) == 1  # default threshold: only the stale one
    with session_scope(world) as s:
        stale, live = repo.get_run(s, stale_id), repo.get_run(s, live_id)
        assert (stale.run_status, live.run_status, repo.get_run(s, ok.run_id).run_status) == ("FAILED", "RUNNING", "COMPLETED")
        statuses = [e.stage_status for e in repo.list_events(s, stale_id)]
        assert statuses[:2] == ["COMPLETED"] * 2 and statuses[2] == "FAILED" and set(statuses[3:]) == {"SKIPPED"}
        assert s.query(Invoice).filter_by(processing_run_id=stale.id).one().final_status == FAILED
        assert fail_abandoned_runs(s) == 0  # idempotent
        s.delete(live)  # the in-progress run is test scaffolding: remove it so the history invariants can run


# ============================================================================ the safety net itself must be able to fail


def test_the_history_invariants_would_catch_an_unsafe_record(world, settings, demo, gen_module):
    """Prove the safety net is not vacuous: corrupt the history on purpose and see it object."""
    r = run_pipeline(
        world,
        settings,
        FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")),
        demo["standard"].path.read_bytes(),
        "a.pdf",
    )
    assert_history_consistent(world)  # healthy
    with session_scope(world) as s:
        run = repo.get_run(s, r.run_id)
        run.run_status, run.overall_error = "FAILED", "boom"  # an APPROVED invoice inside a FAILED run
    with pytest.raises(AssertionError):
        assert_history_consistent(world)
    with session_scope(world) as s:
        run = repo.get_run(s, r.run_id)
        run.run_status, run.overall_error = "COMPLETED", None  # restore for the fixture's own final check


def extraction_from_invoice_stub():
    from invoiceflow.schemas import InvoiceExtraction

    return InvoiceExtraction(invoice_number="NEVER-USED")


_ = pipeline_module, pdf_extractor  # (imported so the patch targets above resolve by dotted path)
