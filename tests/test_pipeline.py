"""Offline end-to-end tests of the pipeline: real PDFs, real OCR, real database, fake AI (never Gemini)."""

import threading
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from conftest import requires_tesseract
from fakes import TEST_DOUBLE_WARNING, FakeAiProvider, ground_truth

from invoiceflow import constants
from invoiceflow.ai.provider import GeminiProvider
from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import ExtractionRecordRow, Invoice, ProcessingRun
from invoiceflow.database.seed import seed_purchase_orders
from invoiceflow.schemas import InvoiceExtraction, PipelineResult
from invoiceflow.services.pipeline import InvoicePipeline

STAGES = [name for name, _ in constants.PIPELINE_STAGES]
APPROVED, REVIEW = "APPROVED", "REVIEW_REQUIRED"


@pytest.fixture
def seeded(db_engine):
    with session_scope(db_engine) as s:
        seed_purchase_orders(s)
    return db_engine


def make(engine, settings, provider=None, **kw):
    return InvoicePipeline(settings, engine=engine, ai_provider=provider, **kw)


def pdf_bytes(demo, key):
    return demo[key].path.read_bytes()


def statuses(result: PipelineResult) -> dict[str, str]:
    return {e.stage_name: e.status for e in result.stages}


def db_events(engine, run_id):
    with session_scope(engine) as s:
        return {e.stage_name: e for e in repo.list_events(s, run_id)}


def good_provider(gen_module, name, **over):
    return FakeAiProvider(ground_truth(gen_module, name, **over))


# ============================================================================ the happy path (acceptance criterion)


def test_one_pdf_travels_through_every_stage_and_is_saved(seeded, settings, demo, gen_module):
    g = demo["standard"]
    provider = good_provider(gen_module, "invoice_standard_digital.pdf")
    result = make(seeded, settings, provider).process(pdf_bytes(demo, "standard"), g.path.name)  # no manual step anywhere

    assert result.run_status == "COMPLETED" and result.error is None and result.persistence_error is None
    assert result.decision_status == APPROVED and result.decision_reasons == []
    assert result.filename == g.path.name and len(result.file_hash) == 64 and result.duration_seconds > 0
    assert result.completed_at >= result.started_at

    # every stage ran and was recorded, in order; only OCR was skipped (the PDF has a text layer)
    assert [e.stage_name for e in result.stages] == STAGES
    assert statuses(result) == {**dict.fromkeys(STAGES, "COMPLETED"), "ocr": "SKIPPED"}
    for ev in result.stages:
        assert ev.title and ev.message
        if ev.status == "COMPLETED":
            assert ev.started_at and ev.completed_at and ev.started_at <= ev.completed_at and ev.duration_ms >= 0
    assert "text layer" in next(e for e in result.stages if e.stage_name == "ocr").message

    # structured response parts
    assert result.invoice.invoice_number == g.invoice_number and result.invoice.grand_total == g.total
    assert result.ocr.needed is False and result.ocr.performed is False and result.ocr.text_method == "text"
    assert result.ingestion.page_count == 1 and g.invoice_number in result.ingestion.text
    assert (result.ai.provider, result.ai.is_test_double, result.ai.model) == ("TEST-DOUBLE", True, "TEST-DOUBLE-NOT-GEMINI")
    assert result.ai.status == "success" and result.ai.attempts == 1 and TEST_DOUBLE_WARNING in result.ai.warnings
    assert result.po_matching.matched_po_number == "PO-2024-1001" and result.po_matching.remaining_balance == 1216
    assert {c.check for c in result.po_matching.checks} >= {"po_found", "vendor_matches_po", "po_is_open", "currency_matches_po"}
    assert {c.check for c in result.validation} >= {"not_duplicate_file", "total_adds_up", "within_po_limit"}
    assert all(c.passed for c in result.validation)
    assert result.decision.summary.startswith("Approved:") and result.decision.matched_po_number == "PO-2024-1001"
    assert len(provider.calls) == 1 and provider.calls[0]["source_method"] == "text"

    # persisted, with nothing manual
    with session_scope(seeded) as s:
        run = repo.get_run(s, result.run_id)
        assert run.run_status == "COMPLETED" and run.completed_at and run.file_hash == result.file_hash
        inv = s.query(Invoice).one()
        assert inv.final_status == "APPROVED" and inv.grand_total == g.total and inv.po_number == "PO-2024-1001"
        assert inv.decision_explanation["status"] == "APPROVED" and inv.decision_reasons == []
        assert inv.extraction_json["ingestion"]["method"] == "text" and inv.extraction_json["invoice"]["invoice_number"] == g.invoice_number
        rec = s.query(ExtractionRecordRow).one()
        assert rec.model == "TEST-DOUBLE-NOT-GEMINI" and rec.status == "success" and rec.file_sha256 == result.file_hash
        rows = {e.stage_name: e for e in repo.list_events(s, result.run_id)}
        for ev in result.stages:
            row = rows[ev.stage_name]
            assert (row.stage_status, row.message, row.duration_ms) == (ev.status, ev.message, ev.duration_ms)


def test_no_pdf_binary_is_ever_stored(seeded, settings, demo, gen_module, db_engine):
    make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf")).process(pdf_bytes(demo, "standard"), "a.pdf")
    assert b"%PDF-" not in Path(db_engine.url.database).read_bytes()  # only hashes and extracted fields are stored


def test_multipage_digital_invoice(seeded, settings, demo, gen_module):
    r = make(seeded, settings, good_provider(gen_module, "invoice_multipage.pdf")).process(pdf_bytes(demo, "multipage"), "m.pdf")
    assert r.run_status == "COMPLETED" and r.decision_status == APPROVED and r.ingestion.page_count >= 2
    assert len(r.invoice.line_items) > 10 and r.po_matching.matched_po_number == "PO-2024-1002"


def test_every_generated_digital_invoice_gets_its_expected_decision(seeded, settings, demo, gen_module):
    expected_review = {"invoice_missing_po.pdf": "MISSING_PO_REFERENCE", "invoice_wayne_closed_po.pdf": "PO_CLOSED"}
    for g in demo["all"]:
        if g.scanned:
            continue
        r = make(seeded, settings, good_provider(gen_module, g.path.name)).process(g.path.read_bytes(), g.path.name)
        assert r.run_status == "COMPLETED", (g.path.name, r.error)
        if g.path.name in expected_review:
            assert r.decision_status == REVIEW and expected_review[g.path.name] in r.decision.reason_codes
        else:
            assert r.decision_status == APPROVED, (g.path.name, r.decision.summary)


@requires_tesseract
def test_scanned_invoice_uses_ocr_end_to_end(seeded, settings, demo, gen_module):
    g = demo["scanned"]
    provider = good_provider(gen_module, "invoice_scanned.pdf", invoice_date=None)
    r = make(seeded, settings, provider).process(g.path.read_bytes(), g.path.name)
    assert r.run_status == "COMPLETED" and statuses(r)["ocr"] == "COMPLETED" and statuses(r)["text_extraction"] == "COMPLETED"
    assert r.ocr.needed and r.ocr.performed and r.ocr.pages == [1] and r.ocr.text_method == "ocr"
    assert r.ocr.confidences[1] > 50 and "OCR read page(s) 1" in next(e for e in r.stages if e.stage_name == "ocr").message
    assert provider.calls[0]["source_method"] == "ocr"  # the AI is told the text came from OCR
    assert r.decision_status == APPROVED and r.invoice.purchase_order_number == "PO-2024-1004"


# ============================================================================ live tracking


def test_events_are_emitted_live_and_visible_in_the_database_as_they_happen(seeded, settings, demo, gen_module):
    seen: list = []
    snapshots: dict = {}

    def on_event(ev):
        seen.append((ev.stage_name, ev.status))
        if ev.status == "RUNNING" and ev.stage_name in ("upload_validation", "text_extraction", "ai_extraction", "normalization"):
            with session_scope(seeded) as s:  # a second connection, mid-run, like a live UI
                run = s.query(ProcessingRun).one()
                snapshots[ev.stage_name] = {e.stage_name: e.stage_status for e in repo.list_events(s, run.run_id)}

    r = make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf")).process(
        pdf_bytes(demo, "standard"), "a.pdf", on_event=on_event
    )
    assert seen[: len(STAGES)] == [(s, "PENDING") for s in STAGES]  # the whole plan is announced first
    # each stage is announced RUNNING before its terminal state
    for name in STAGES:
        states = [st for s, st in seen if s == name]
        assert states[0] == "PENDING" and states[-1] == r.stages[STAGES.index(name)].status
        if "COMPLETED" in states:
            assert states.index("RUNNING") < states.index("COMPLETED")
    # while a stage was running, the database already showed it RUNNING and the stages before it COMPLETED
    assert snapshots["upload_validation"]["upload_validation"] == "RUNNING" and snapshots["upload_validation"]["file_hash"] == "COMPLETED"
    assert snapshots["text_extraction"]["upload_validation"] == "COMPLETED" and snapshots["text_extraction"]["ai_extraction"] == "PENDING"
    assert snapshots["ai_extraction"]["text_extraction"] == "COMPLETED" and snapshots["ai_extraction"]["ai_extraction"] == "RUNNING"
    assert snapshots["normalization"]["structure_validation"] == "COMPLETED"


def test_a_broken_ui_callback_cannot_break_the_pipeline(seeded, settings, demo, gen_module):
    def boom(ev):
        raise RuntimeError("UI exploded")

    r = make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf")).process(
        pdf_bytes(demo, "standard"), "a.pdf", on_event=boom
    )
    assert r.run_status == "COMPLETED" and r.decision_status == APPROVED


# ============================================================================ decisions flow through


def test_missing_po_invoice_completes_with_review(seeded, settings, demo, gen_module):
    r = make(seeded, settings, good_provider(gen_module, "invoice_missing_po.pdf")).process(pdf_bytes(demo, "missing_po"), "a.pdf")
    assert r.run_status == "COMPLETED" and r.decision_status == REVIEW
    assert r.po_matching.matched_po_number is None and [x.po_number for x in r.po_matching.suggestions] == ["PO-2024-1001"]
    assert statuses(r)["po_matching"] == "COMPLETED" and "MISSING_PO_REFERENCE" in [x.code for x in r.decision_reasons]
    assert any("PO problem" in (e.message or "") for e in r.stages if e.stage_name == "po_matching")


def test_uploading_the_same_invoice_twice_blocks_the_second(seeded, settings, demo, gen_module):
    pipe = make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf"))
    first = pipe.process(pdf_bytes(demo, "standard"), "a.pdf")
    second = pipe.process(pdf_bytes(demo, "standard"), "again.pdf")
    assert (first.decision_status, second.decision_status) == (APPROVED, "DUPLICATE_BLOCKED")
    assert second.run_status == "COMPLETED"  # the pipeline worked; the invoice was blocked
    assert {"DUPLICATE_FILE", "DUPLICATE_INVOICE_NUMBER"} <= set(second.decision.reason_codes)
    assert "Duplicate found" in next(e for e in second.stages if e.stage_name == "duplicate_check").message
    with session_scope(seeded) as s:
        assert [i.final_status for i in s.query(Invoice).order_by(Invoice.id)] == ["APPROVED", "DUPLICATE_BLOCKED"]
        assert len(repo.get_approved_invoices_for_po(s, "PO-2024-1001")) == 1


def test_extraction_that_cannot_be_verified_against_the_document_is_never_approved(seeded, settings, demo, gen_module):
    invented = good_provider(gen_module, "invoice_standard_digital.pdf", purchase_order_number="PO-2024-1002", grand_total="1500.00")
    r = make(seeded, settings, invented).process(pdf_bytes(demo, "standard"), "a.pdf")
    assert r.decision_status == REVIEW and "EXTRACTION_UNCERTAIN" in r.decision.reason_codes


# ============================================================================ failures


def assert_failed_safely(engine, result, *, stage, code):
    """Invariants for every failed run."""
    assert result.run_status == "FAILED" and result.decision_status == "PROCESSING_FAILED"
    assert result.error.stage == stage and result.error.code == code and result.error.message and result.error.action
    st = statuses(result)
    assert st[stage] == "FAILED"
    order = STAGES.index(stage)
    for name in STAGES[order + 1 :]:
        assert st[name] in ("SKIPPED", "COMPLETED") and (st[name] == "SKIPPED" or name == "save_result"), (name, st)
    assert all(e.status not in ("PENDING", "RUNNING") for e in result.stages)
    if order + 1 < len(STAGES) and STAGES[order + 1] != "save_result":
        assert "Not run because" in next(e for e in result.stages if e.stage_name == STAGES[order + 1]).message
    with session_scope(engine) as s:
        run = repo.get_run(s, result.run_id)
        assert run.run_status == "FAILED" and run.overall_error and run.completed_at
        inv = s.query(Invoice).filter_by(processing_run_id=run.id).one()
        assert inv.final_status == "PROCESSING_FAILED" and inv.decision_reasons == ["PROCESSING_FAILED"]
        assert s.query(Invoice).filter(Invoice.final_status == "APPROVED").count() == 0
        rows = {e.stage_name: e.stage_status for e in repo.list_events(s, result.run_id)}
        assert rows == {**st} and rows[stage] == "FAILED"
    return st


def test_text_file_renamed_to_pdf_is_rejected_and_recorded(seeded, settings):
    provider = FakeAiProvider(InvoiceExtraction(invoice_number="X"))
    r = make(seeded, settings, provider).process(b"just some text", "invoice.pdf")
    st = assert_failed_safely(seeded, r, stage="upload_validation", code="bad_signature")
    assert st["file_hash"] == "COMPLETED" and st["text_extraction"] == "SKIPPED"
    assert provider.calls == [] and r.ingestion is None and r.invoice is None and r.ai.used is False
    assert "real PDF" in r.error.message and "Export or scan" in r.error.action


@pytest.mark.parametrize(
    "make_bytes, filename, code",
    [
        (lambda: b"%PDF-1.7\n" + b"\x00\xff garbage " * 50, "bad.pdf", "corrupt_pdf"),
        (lambda: b"", "empty.pdf", "empty_file"),
        (lambda: b"%PDF-1.4 fake", "fake.pdf", "corrupt_pdf"),
    ],
)
def test_other_bad_uploads(seeded, settings, make_bytes, filename, code):
    r = make(seeded, settings, FakeAiProvider(InvoiceExtraction(invoice_number="X"))).process(make_bytes(), filename)
    assert_failed_safely(seeded, r, stage="upload_validation", code=code)


def test_wrong_extension_and_encrypted_and_too_many_pages(seeded, settings, demo):
    provider = FakeAiProvider(InvoiceExtraction(invoice_number="X"))
    pipe = make(seeded, settings, provider)
    assert_failed_safely(seeded, pipe.process(pdf_bytes(demo, "standard"), "invoice.docx"), stage="upload_validation", code="bad_extension")
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "secret")
        locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    assert_failed_safely(seeded, pipe.process(locked, "locked.pdf"), stage="upload_validation", code="encrypted")
    tiny = replace(settings, max_pdf_pages=1)
    r = make(seeded, tiny, provider).process(pdf_bytes(demo, "multipage"), "big.pdf")
    assert_failed_safely(seeded, r, stage="upload_validation", code="too_many_pages")
    assert provider.calls == []


def test_blank_pdf_fails_at_text_extraction(seeded, settings):
    with pymupdf.open() as doc:
        doc.new_page()
        blank = doc.tobytes()
    r = make(seeded, settings, FakeAiProvider(InvoiceExtraction(invoice_number="X"))).process(blank, "blank.pdf")
    assert_failed_safely(seeded, r, stage="text_extraction", code="no_text")


def test_missing_tesseract_is_an_actionable_failure_that_keeps_the_partial_text(seeded, settings, demo):
    broken = replace(settings, tesseract_cmd=r"C:\definitely\missing\tesseract.exe")
    provider = FakeAiProvider(InvoiceExtraction(invoice_number="X"))
    r = make(seeded, broken, provider).process(pdf_bytes(demo, "scanned"), "scan.pdf")
    st = assert_failed_safely(seeded, r, stage="ocr", code="ocr_unavailable")
    assert st["upload_validation"] == st["text_extraction"] == "COMPLETED"  # earlier work is kept as COMPLETED
    assert "TESSERACT_CMD" in r.error.message and "Install Tesseract" in r.error.action and r.error.manual_entry_possible is False
    assert r.ingestion is not None and r.ingestion.pages[0].method == "none"  # the partial result is preserved
    assert r.ocr.needed and r.ocr.performed is False and r.ocr.pages == [1]
    assert provider.calls == []
    with session_scope(seeded) as s:
        inv = s.query(Invoice).one()
        assert inv.extraction_json["failed_stage"] == "ocr" and inv.extraction_json["ingestion"]["page_count"] == 1


def test_ai_outage_fails_the_run_but_keeps_text_and_the_ai_audit_record(seeded, settings, demo):
    down = FakeAiProvider(
        fail=("unavailable", "service_unavailable", "Gemini is temporarily unavailable (HTTP 503). (gave up after 3 attempts)")
    )
    r = make(seeded, settings, down).process(pdf_bytes(demo, "standard"), "a.pdf")
    st = assert_failed_safely(seeded, r, stage="ai_extraction", code="service_unavailable")
    assert st["text_extraction"] == "COMPLETED" and st["ocr"] == "SKIPPED"
    assert r.error.manual_entry_possible is True and "enter the invoice fields manually" in r.error.action
    assert r.ingestion is not None and demo["standard"].invoice_number in r.ingestion.text  # extracted text preserved
    assert r.invoice is None and r.decision.status == "PROCESSING_FAILED"
    assert (r.ai.status, r.ai.error_code, r.ai.attempts, r.ai.used) == ("unavailable", "service_unavailable", 3, True)
    with session_scope(seeded) as s:
        rec = s.query(ExtractionRecordRow).one()  # the failed AI attempt is itself kept
        assert rec.status == "unavailable" and rec.error_code == "service_unavailable" and rec.model == "TEST-DOUBLE-NOT-GEMINI"
        assert s.query(Invoice).one().extraction_json["ai_error"]["code"] == "service_unavailable"


def test_default_provider_without_api_key_degrades_honestly_and_never_uses_the_fake(seeded, settings, demo):
    from dataclasses import replace as rep

    no_key = rep(settings, gemini_api_key=None)
    pipe = InvoicePipeline(no_key, engine=seeded)  # default provider
    assert isinstance(pipe.provider, GeminiProvider) and pipe.provider.is_test_double is False and pipe.provider.name == "gemini"
    r = pipe.process(pdf_bytes(demo, "standard"), "a.pdf")
    assert_failed_safely(seeded, r, stage="ai_extraction", code="config_error")
    assert r.ai.provider == "gemini" and r.ai.is_test_double is False and "GEMINI_API_KEY" in r.error.message


def test_invalid_ai_answer_and_empty_extraction(seeded, settings, demo):
    bad = FakeAiProvider(fail=("failed", "invalid_response", "Gemini's answer was not valid JSON."))
    assert_failed_safely(
        seeded, make(seeded, settings, bad).process(pdf_bytes(demo, "standard"), "a.pdf"), stage="ai_extraction", code="invalid_response"
    )
    empty = FakeAiProvider(InvoiceExtraction())
    r = make(seeded, settings, empty).process(pdf_bytes(demo, "standard"), "b.pdf")
    st = assert_failed_safely(seeded, r, stage="structure_validation", code="empty_extraction")
    assert st["ai_extraction"] == "COMPLETED" and r.error.manual_entry_possible is True


def test_a_crashing_provider_is_contained_and_recorded(seeded, settings, demo):
    r = make(seeded, settings, FakeAiProvider(crash=ValueError("provider bug"))).process(pdf_bytes(demo, "standard"), "a.pdf")
    assert_failed_safely(seeded, r, stage="ai_extraction", code="internal_error")
    assert "ValueError" in r.error.message and "provider bug" not in r.error.message  # no internals leaked to the user


def test_database_failure_while_saving_rolls_back_cleanly_and_keeps_history(seeded, settings, demo, gen_module, monkeypatch):
    real_save = repo.save_invoice

    def flaky(session, run_id, normalized, *, final_status, **kw):
        if final_status != "PROCESSING_FAILED":
            raise RuntimeError("disk full")
        return real_save(session, run_id, normalized, final_status=final_status, **kw)

    monkeypatch.setattr(repo, "save_invoice", flaky)
    r = make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf")).process(pdf_bytes(demo, "standard"), "a.pdf")
    st = assert_failed_safely(seeded, r, stage="save_result", code="internal_error")
    # the decision genuinely ran before the save failed, so those stages are honestly COMPLETED...
    assert all(st[s] == "COMPLETED" for s in ("duplicate_check", "po_matching", "financial_validation", "decision"))
    # ...and their events survived the rollback, as did the AI record and the extracted data
    with session_scope(seeded) as s:
        assert s.query(ExtractionRecordRow).count() == 1
        inv = s.query(Invoice).one()
        assert inv.final_status == "PROCESSING_FAILED" and inv.invoice_number == demo["standard"].invoice_number  # data preserved
        assert repo.get_approved_invoices_for_po(s, "PO-2024-1001") == []


def test_a_failed_run_never_disturbs_earlier_history(seeded, settings, demo, gen_module):
    ok = make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf")).process(pdf_bytes(demo, "standard"), "ok.pdf")
    bad = make(seeded, settings, FakeAiProvider(InvoiceExtraction(invoice_number="X"))).process(b"nope", "bad.pdf")
    assert (ok.decision_status, bad.decision_status) == (APPROVED, "PROCESSING_FAILED")
    with session_scope(seeded) as s:
        assert s.query(ProcessingRun).count() == 2 and s.query(Invoice).count() == 2
        assert repo.get_run(s, ok.run_id).run_status == "COMPLETED"
        assert [i.invoice_number for i in repo.get_approved_invoices_for_po(s, "PO-2024-1001")] == [demo["standard"].invoice_number]
        assert {e.stage_status for e in repo.list_events(s, ok.run_id)} == {"COMPLETED", "SKIPPED"}


# ============================================================================ manual entry after an AI failure


def test_manual_entry_after_an_ai_outage_uses_the_same_pipeline_and_rules(seeded, settings, demo, gen_module):
    down = FakeAiProvider(fail=("unavailable", "service_unavailable", "Gemini is temporarily unavailable"))
    failed = make(seeded, settings, down).process(pdf_bytes(demo, "standard"), "a.pdf")
    assert failed.run_status == "FAILED" and failed.error.manual_entry_possible

    provider = FakeAiProvider(InvoiceExtraction(invoice_number="SHOULD-NOT-BE-CALLED"))
    manual = ground_truth(gen_module, "invoice_standard_digital.pdf")
    r = make(seeded, settings, provider).process(pdf_bytes(demo, "standard"), "a.pdf", manual_extraction=manual)
    assert provider.calls == []  # the AI was not consulted
    assert r.run_status == "COMPLETED" and r.decision_status == APPROVED  # the earlier failed attempt is not a duplicate
    assert (
        statuses(r)["ai_extraction"] == "SKIPPED" and "Manual entry" in next(e for e in r.stages if e.stage_name == "ai_extraction").message
    )
    assert (r.ai.source, r.ai.status, r.ai.used, r.ai.model) == ("manual", "manual", False, None)
    assert r.run_id != failed.run_id and r.file_hash == failed.file_hash
    with session_scope(seeded) as s:
        assert s.query(ProcessingRun).count() == 2  # the failed attempt stays in the history
        assert sorted(i.final_status for i in s.query(Invoice)) == ["APPROVED", "PROCESSING_FAILED"]
        assert {x.source for x in s.query(ExtractionRecordRow)} == {"ai", "manual"}


def test_manual_values_are_validated_like_ai_values(seeded, settings, demo, gen_module):
    wrong_total = ground_truth(gen_module, "invoice_standard_digital.pdf", grand_total="9999.00")
    r = make(seeded, settings, FakeAiProvider(InvoiceExtraction())).process(
        pdf_bytes(demo, "standard"), "a.pdf", manual_extraction=wrong_total
    )
    assert r.decision_status == REVIEW and "TOTAL_MISMATCH" in r.decision.reason_codes


# ============================================================================ safety


def test_the_fake_provider_is_refused_outside_automated_tests(seeded, settings, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="test double"):
        InvoicePipeline(settings, engine=seeded, ai_provider=FakeAiProvider(InvoiceExtraction(invoice_number="X")))
    assert InvoicePipeline(settings, engine=seeded).provider.is_test_double is False  # the default is the real provider


def test_no_test_double_exists_inside_the_application_package():
    root = Path(__file__).resolve().parents[1] / "invoiceflow"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "is_test_double = True" not in text and "TEST-DOUBLE" not in text, path


def test_nothing_is_ever_approved_when_a_stage_fails(seeded, settings, demo, gen_module):
    pipe = make(seeded, settings, FakeAiProvider(fail=("failed", "blocked", "blocked")))
    for _ in range(3):
        assert pipe.process(pdf_bytes(demo, "standard"), "a.pdf").decision_status == "PROCESSING_FAILED"
    with session_scope(seeded) as s:
        assert s.query(Invoice).filter(Invoice.final_status == "APPROVED").count() == 0
        assert s.query(Invoice).count() == 3  # three failed runs, each kept (retries are never duplicates)


def test_simultaneous_uploads_of_one_invoice_approve_it_only_once(seeded, settings, demo, gen_module):
    data = pdf_bytes(demo, "standard")
    results, errors = [None, None], []
    barrier = threading.Barrier(2)

    def worker(i):
        try:
            pipe = make(seeded, settings, good_provider(gen_module, "invoice_standard_digital.pdf"))
            barrier.wait(timeout=10)
            results[i] = pipe.process(data, f"copy{i}.pdf")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=90) for t in threads]
    assert not errors, errors
    assert sorted(r.decision_status for r in results) == [APPROVED, "DUPLICATE_BLOCKED"]
    assert all(r.run_status == "COMPLETED" for r in results)
