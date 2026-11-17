"""The navigation shell and the Process Invoice page: upload, live run, result, failures, repeat-click safety."""

import time
from dataclasses import replace

import pymupdf
import pytest
import streamlit as st
from fakes import FakeAiProvider, ground_truth
from streamlit.testing.v1 import AppTest
from ui_helpers import alltext, html, no_exceptions, page

from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import Invoice, ProcessingEvent, ProcessingRun
from invoiceflow.services.pipeline import InvoicePipeline
from invoiceflow.ui import app_shell, context, dashboard, health_page, history_page, orders_page, process_page, theme
from invoiceflow.ui.jobs import PipelineJob

APP = "D:/Documents/invoiceflow/app.py"


# ============================================================================ shell and navigation


def test_shell_defines_the_five_pages_and_the_sidebar(ui_env, monkeypatch):
    captured = {}

    class Router:
        def __init__(self, fn):
            self.fn = fn

        def run(self):
            self.fn()

    def fake_navigation(pages, **kw):
        captured["pages"] = list(pages)
        return Router(captured["chosen"])

    monkeypatch.setattr(st, "navigation", fake_navigation)
    expected = {
        "Dashboard": (dashboard.render, "dashboard"),
        "Process Invoice": (process_page.render, "process"),
        "Purchase Orders": (orders_page.render, "orders"),
        "Processing History": (history_page.render, "history"),
        "About / System Health": (health_page.render, "health"),
    }
    heading = {
        "Dashboard": "Dashboard",
        "Process Invoice": "Process Invoice",
        "Purchase Orders": "Purchase Orders",
        "Processing History": "Processing History",
        "About / System Health": "About & System Health",
    }
    for title, (fn, _) in expected.items():
        captured["chosen"] = fn
        at = AppTest.from_file(APP, default_timeout=60).run()
        no_exceptions(at)
        assert [p.title for p in captured["pages"]] == list(expected)  # same order, every run
        assert at.title[0].value == heading[title], title
        side = "\n".join(m.value for m in at.sidebar.markdown)
        assert "Demo · synthetic data" in side and "Not a production payment-approval system" in side and "paid or posted" in side
        assert "Demonstration product" in html(at)  # the synthetic-data label is on every page
    assert [p.url_path for p in captured["pages"]] == ["", "process", "orders", "history", "health"]  # the default page serves "/"
    assert captured["pages"][0]._default is True and set(context.PAGES) == {"dashboard", "process", "orders", "history", "health"}


def test_real_app_entry_point_renders_the_default_page(ui_env):
    at = AppTest.from_file(APP, default_timeout=60).run()
    no_exceptions(at)
    assert at.title[0].value == "Dashboard" and "<style>" in at.markdown[0].value  # stylesheet injected once


def test_logo_asset_exists_and_is_wired():
    assert app_shell.LOGO.exists() and "InvoiceFlow" in app_shell.LOGO.read_text(encoding="utf-8")


# ============================================================================ process page: helpers


@pytest.fixture
def use_fake(monkeypatch, ui_env):
    """Make the UI's pipeline use a test double (the real UI always builds the Gemini provider)."""

    def install(provider):
        monkeypatch.setattr(context, "make_pipeline", lambda settings: InvoicePipeline(settings, ai_provider=provider))
        return provider

    return install


def runs_in_db(engine) -> list[str]:
    with session_scope(engine) as s:
        return [i.final_status for i in s.query(Invoice).order_by(Invoice.id)]


def upload(at, name, data):
    at.file_uploader(key="uploader_0").upload(name, data, "application/pdf").run()
    return at


def process(at):
    at.button(key="process_btn").click().run()
    return at


# ============================================================================ process page: before processing


def test_process_page_without_a_file(seeded):
    at = page("process_page")
    no_exceptions(at)
    assert at.title[0].value == "Process Invoice" and at.button(key="process_btn").disabled is True
    assert at.button(key="process_btn").label == "Process invoice"
    assert "do not upload real invoices" in html(at).lower()  # the privacy/synthetic notice


def test_valid_upload_shows_file_details_preview_and_enables_processing(seeded, demo):
    at = page("process_page")
    upload(at, "inv.pdf", demo["standard"].path.read_bytes())
    no_exceptions(at)
    text = html(at)
    assert "inv.pdf" in text and "Pages" in text and "SHA-256" in text and "passed the pre-checks" in alltext(at)
    assert len(at.get("image")) == 1  # a page preview is rendered
    assert at.button(key="process_btn").disabled is False


def test_multipage_upload_offers_a_page_selector(seeded, demo):
    at = upload(page("process_page"), "m.pdf", demo["multipage"].path.read_bytes())
    assert len(at.get("image")) == 1 and at.selectbox(key="preview_page").options[0].startswith("Page 1 of")
    at.selectbox(key="preview_page").select(1).run()
    no_exceptions(at)


@pytest.mark.parametrize(
    "data, name, message",
    [
        (b"this is plain text", "notes.pdf", "not a real PDF"),
        (b"", "empty.pdf", "empty"),
        (b"%PDF-1.7\n" + b"\x00\xff junk" * 40, "broken.pdf", "corrupt"),
    ],
)
def test_invalid_uploads_show_clear_messages_and_cannot_be_processed(seeded, data, name, message):
    at = upload(page("process_page"), name, data)
    no_exceptions(at)
    assert any("This file cannot be processed" in e.value and message in e.value for e in at.error), [e.value for e in at.error]
    assert at.button(key="process_btn").disabled is True
    assert at.get("image") == []


def test_page_limit_message(seeded, demo, monkeypatch):
    monkeypatch.setenv("MAX_PDF_PAGES", "1")
    at = upload(page("process_page"), "m.pdf", demo["multipage"].path.read_bytes())
    assert any("page limit" in e.value for e in at.error) and at.button(key="process_btn").disabled is True


def test_encrypted_pdf_message(seeded):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "x")
        locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    at = upload(page("process_page"), "locked.pdf", locked)
    assert any("password-protected" in e.value for e in at.error)


# ============================================================================ process page: running it


def test_processing_an_invoice_end_to_end_through_the_page(seeded, demo, gen_module, use_fake):
    provider = use_fake(FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")))
    g = demo["standard"]
    at = upload(page("process_page"), g.path.name, g.path.read_bytes())
    process(at)
    no_exceptions(at)

    # the result is rendered from what is stored
    text = html(at)
    assert (
        "Approved by the automated checks" in text and f"Invoice {g.invoice_number}" in text and "Result" in [s.value for s in at.subheader]
    )
    assert [t.label for t in at.tabs] == [
        "Financials & PO",
        "Validation checks",
        "Invoice details",
        "Line items",
        "Execution history",
        "Source & AI",
    ]
    assert "Database persistence" in text and "Saved invoice as APPROVED" in text  # the real stage history
    assert "PO-2024-1001" in "\n".join(str(d.value) for d in at.dataframe)
    assert len(provider.calls) == 1
    assert runs_in_db(seeded) == ["APPROVED"]
    with session_scope(seeded) as s:
        assert s.query(ProcessingRun).one().run_status == "COMPLETED" and s.query(ProcessingEvent).count() == 12

    # the button now says so and is disabled
    assert at.button(key="process_btn").label == "Processed ✓" and at.button(key="process_btn").disabled is True


def test_repeated_interactions_never_create_a_second_run(seeded, demo, gen_module, use_fake):
    use_fake(FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")))
    at = upload(page("process_page"), "a.pdf", demo["standard"].path.read_bytes())
    process(at)
    for _ in range(4):  # reruns from any widget interaction, navigation back and forth, impatient clicking
        at.run()
    assert at.button(key="process_btn").disabled is True  # a browser user cannot click it at all
    assert runs_in_db(seeded) == ["APPROVED"]
    with session_scope(seeded) as s:
        assert s.query(ProcessingRun).count() == 1


def test_a_click_while_a_run_is_active_attaches_to_it_instead_of_starting_another(seeded, demo, gen_module, use_fake):
    class Slow(FakeAiProvider):
        def extract(self, document_text, *, source_method):
            time.sleep(0.4)  # a slow API (this sleep belongs to the test double, not to the UI)
            return super().extract(document_text, source_method=source_method)

    provider = use_fake(Slow(ground_truth(gen_module, "invoice_standard_digital.pdf")))
    data = demo["standard"].path.read_bytes()
    at = upload(page("process_page"), "a.pdf", data)
    at.session_state["process_state"] = {
        "n": 0,
        "job": None,
        "start": None,
        "run_id": None,
        "done_key": None,
        "data": data,
        "filename": "a.pdf",
        "ctype": "application/pdf",
        "crash": None,
    }
    state = at.session_state["process_state"]
    state["job"] = PipelineJob(
        InvoicePipeline(replace(__import__("invoiceflow.config", fromlist=["x"]).load_settings()), ai_provider=provider),
        data,
        "a.pdf",
        "application/pdf",
        key="k",
    )
    state["start"] = {"manual": None}  # a second "start" request arriving while the job is running
    at.run()
    no_exceptions(at)
    assert len(provider.calls) == 1 and runs_in_db(seeded) == ["APPROVED"]  # one run, not two
    with session_scope(seeded) as s:
        assert s.query(ProcessingRun).count() == 1


def test_a_duplicate_upload_is_blocked_and_explained(seeded, demo, gen_module, use_fake):
    use_fake(FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")))
    data = demo["standard"].path.read_bytes()
    process(upload(page("process_page"), "a.pdf", data))
    at = page("process_page")  # a fresh browser session uploads the same invoice again
    process(upload(at, "again.pdf", data))
    text = html(at)
    assert "Duplicate blocked" in text and "already processed before" in text and "must not be paid again" in text
    assert runs_in_db(seeded) == ["APPROVED", "DUPLICATE_BLOCKED"]


def test_review_result_tells_the_reviewer_what_to_verify(seeded, demo, gen_module, use_fake):
    use_fake(FakeAiProvider(ground_truth(gen_module, "invoice_missing_po.pdf")))
    at = process(upload(page("process_page"), "m.pdf", demo["missing_po"].path.read_bytes()))
    text = html(at)
    assert "Review required: not approved" in text and "What a reviewer must verify" in text
    assert "does not show a purchase order number" in text
    assert any("Find out which purchase order" in c.label for c in at.checkbox)
    assert "Approving or rejecting is not available" in alltext(at)
    assert not any("approve" in b.label.lower() for b in at.button)  # no financial action is offered to the public
    assert runs_in_db(seeded) == ["REVIEW_REQUIRED"]


def test_failure_shows_an_actionable_error_and_keeps_the_record(seeded, demo, use_fake):
    use_fake(FakeAiProvider(fail=("unavailable", "service_unavailable", "Gemini is temporarily unavailable (HTTP 503).")))
    at = process(upload(page("process_page"), "a.pdf", demo["standard"].path.read_bytes()))
    no_exceptions(at)
    text = html(at)
    assert "Processing failed: nothing was approved" in text and "Gemini is temporarily unavailable" in text
    assert "enter the invoice fields manually" in text and "Stage history" not in text
    assert "Not run because" in text  # the skipped stages are shown honestly
    assert runs_in_db(seeded) == ["PROCESSING_FAILED"]


def test_manual_entry_after_an_ai_outage(seeded, demo, gen_module, use_fake):
    use_fake(FakeAiProvider(fail=("unavailable", "service_unavailable", "Gemini is temporarily unavailable (HTTP 503).")))
    truth = ground_truth(gen_module, "invoice_standard_digital.pdf")
    at = process(upload(page("process_page"), "a.pdf", demo["standard"].path.read_bytes()))
    assert any(b.label == "Use these values" for b in at.button)
    for name in (
        "invoice_number",
        "invoice_date",
        "purchase_order_number",
        "vendor_name",
        "currency",
        "subtotal",
        "tax_total",
        "grand_total",
    ):
        next(t for t in at.text_input if t.key == f"manual_{name}").set_value(getattr(truth, name))
    next(b for b in at.button if b.label == "Use these values").click().run()
    no_exceptions(at)
    # no line items were typed, so the same rules send it to review: it is never approved on thin data
    assert runs_in_db(seeded) == ["PROCESSING_FAILED", "REVIEW_REQUIRED"]
    assert "Manual entry" in html(at) and "NO_LINE_ITEMS" not in html(at)


def test_process_another_invoice_resets_the_page(seeded, demo, gen_module, use_fake):
    use_fake(FakeAiProvider(ground_truth(gen_module, "invoice_standard_digital.pdf")))
    at = process(upload(page("process_page"), "a.pdf", demo["standard"].path.read_bytes()))
    assert "Approved by the automated checks" in html(at)
    at.button(key="another").click().run()
    no_exceptions(at)
    assert "Approved by the automated checks" not in html(at) and at.file_uploader(key="uploader_1") is not None
    assert at.button(key="process_btn").disabled is True  # waiting for a new file


def test_a_crashing_run_is_reported_not_raised(seeded, demo, use_fake):
    use_fake(FakeAiProvider(crash=ValueError("provider bug")))
    at = process(upload(page("process_page"), "a.pdf", demo["standard"].path.read_bytes()))
    no_exceptions(at)
    assert "Processing failed" in html(at) and "provider bug" not in html(at)  # contained and recorded; internals not shown
    assert runs_in_db(seeded) == ["PROCESSING_FAILED"]


# ============================================================================ the live view's data source


def test_job_exposes_real_stage_events_while_the_pipeline_runs(seeded, demo, gen_module):
    observed = []

    class Slow(FakeAiProvider):
        def extract(self, document_text, *, source_method):
            time.sleep(0.5)  # the "API call"; the UI must be able to see this stage RUNNING
            return super().extract(document_text, source_method=source_method)

    from invoiceflow.config import load_settings

    provider = Slow(ground_truth(gen_module, "invoice_standard_digital.pdf"))
    job = PipelineJob(
        InvoicePipeline(load_settings(), ai_provider=provider), demo["standard"].path.read_bytes(), "a.pdf", "application/pdf", key="k"
    )
    while not job.done.wait(0.05):
        observed.append({e.stage_name: e.status for e in job.snapshot()})
    final = {e.stage_name: e.status for e in job.snapshot()}

    mid = [o for o in observed if o["ai_extraction"] == "RUNNING"]
    assert mid, "the AI stage was never seen running"
    first = mid[0]
    assert first["upload_validation"] == first["text_extraction"] == "COMPLETED"  # earlier stages already finished
    assert first["decision"] == first["save_result"] == "PENDING"  # later stages had not started
    assert observed[0]["ai_extraction"] in ("PENDING", "RUNNING") and job.result.decision_status == "APPROVED"
    assert all(s in ("COMPLETED", "SKIPPED") for s in final.values()) and job.exception is None
    # what the page showed is exactly what was stored
    with session_scope(seeded) as s:
        stored = {e.stage_name: e.stage_status for e in s.query(ProcessingEvent)}
    assert stored == final


def test_job_reports_a_crash_instead_of_hanging():
    class Broken:
        name, is_test_double = "x", False

        def process(self, *a, **k):
            raise RuntimeError("database vanished")

    job = PipelineJob(Broken(), b"x", "a.pdf", None, key="k")  # type: ignore[arg-type]
    assert job.done.wait(5) and isinstance(job.exception, RuntimeError) and job.result is None


def test_stage_list_marks_each_state_and_escapes_untrusted_text():
    from invoiceflow.schemas import StageEvent

    events = [
        StageEvent(stage_name="a", title="Done stage", status="COMPLETED", message="ok", duration_ms=1234),
        StageEvent(stage_name="b", title="Live stage", status="RUNNING"),
        StageEvent(stage_name="c", title="Bad stage", status="FAILED", message='<script>alert("x")</script> & "quotes"', duration_ms=5),
        StageEvent(stage_name="d", title="Skipped stage", status="SKIPPED", message="not needed", duration_ms=0),
        StageEvent(stage_name="e", title="Waiting stage", status="PENDING"),
    ]
    out = theme.stage_list_html(events)
    assert "<script>" not in out and "&lt;script&gt;" in out
    for token in ("1,234 ms", "running…", "✔", "▶", "✖", "↷", "○", "Waiting"):
        assert token in out
    assert out.count("if-stage ") == 5
