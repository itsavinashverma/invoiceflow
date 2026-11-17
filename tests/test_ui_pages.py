"""Drive every Streamlit page with real data in a real (temporary) database. The AI is the test double."""

from datetime import date

import pytest
from fakes import FakeAiProvider, ground_truth
from streamlit.testing.v1 import AppTest
from ui_helpers import alltext, html, last_df, metric, no_exceptions, page

from invoiceflow.database import connection
from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import Invoice, ProcessingRun, PurchaseOrder
from invoiceflow.schemas import InvoiceExtraction
from invoiceflow.services.pipeline import InvoicePipeline


@pytest.fixture
def populated(seeded, settings, demo, gen_module):
    """Four real runs: approved, review (no PO), duplicate (same file again), failed (not a PDF)."""

    def run(name, data, fname):
        return InvoicePipeline(settings, engine=seeded, ai_provider=FakeAiProvider(ground_truth(gen_module, name))).process(data, fname)

    std, miss = demo["standard"].path.read_bytes(), demo["missing_po"].path.read_bytes()
    results = [
        run("invoice_standard_digital.pdf", std, "std.pdf"),
        run("invoice_missing_po.pdf", miss, "missing.pdf"),
        run("invoice_standard_digital.pdf", std, "std_again.pdf"),
        InvoicePipeline(settings, engine=seeded, ai_provider=FakeAiProvider(InvoiceExtraction(invoice_number="X"))).process(
            b"nope", "bad.pdf"
        ),
    ]
    return seeded, results


# ---------------------------------------------------------------- dashboard


def test_dashboard_empty_state_with_no_records(seeded):
    at = page("dashboard")
    no_exceptions(at)
    assert at.title[0].value == "Dashboard"
    assert metric(at, "Invoices processed") == "0" and metric(at, "Approved") == "0" and metric(at, "Invoice amount processed") == "—"
    assert "No invoices have been processed yet" in html(at) and "Demonstration product" in html(at)
    assert len(at.dataframe) == 0  # no fake tables or demo numbers


def test_dashboard_metrics_come_from_the_database(populated):
    at = page("dashboard")
    no_exceptions(at)
    assert metric(at, "Invoices processed") == "4"
    assert (metric(at, "Approved"), metric(at, "Review required"), metric(at, "Duplicates blocked"), metric(at, "Processing failures")) == (
        "1",
        "1",
        "1",
        "1",
    )
    assert metric(at, "Invoice amount processed") == "$1,764.43"  # 1,301.12 approved + 463.31 in review; duplicate and failure not counted
    text = html(at)
    assert "if-bar" in text and "Approved" in text and "25%" in text  # status distribution with direct labels
    chart_table, recent, history = at.dataframe[0].value, at.dataframe[1].value, at.dataframe[2].value
    assert list(chart_table["Invoices"]) == [1, 1, 1, 1]  # the chart's table view has the same numbers
    assert list(recent["File"]) == ["bad.pdf", "std_again.pdf", "missing.pdf", "std.pdf"] and len(history) == 4
    assert "✔ Approved" in list(history["Decision"]) and "⛔ Duplicate blocked" in list(
        history["Decision"]
    )  # icon + label, never colour alone


def test_dashboard_metrics_change_when_data_changes(populated):
    before = metric(page("dashboard"), "Invoices processed")
    with session_scope(populated[0]) as s:
        run = s.query(ProcessingRun).first()
        s.delete(run)
    after = metric(page("dashboard"), "Invoices processed")
    assert (before, after) == ("4", "3")


def test_dashboard_search_and_status_and_date_filters(populated):
    at = page("dashboard")
    at.text_input(key="dash_q").set_value("ACME-INV-20433").run()
    assert list(last_df(at)["File"]) == ["missing.pdf"]
    at.text_input(key="dash_q").set_value("zzz-nothing").run()
    assert "No runs match these filters." in alltext(at)
    at.text_input(key="dash_q").set_value("").run()
    at.multiselect(key="dash_status").set_value(["APPROVED", "DUPLICATE_BLOCKED"]).run()
    assert sorted(last_df(at)["File"]) == ["std.pdf", "std_again.pdf"]
    at.multiselect(key="dash_status").set_value([]).run()
    at.selectbox(key="dash_when").select("Today").run()  # all four runs were made today (UTC)
    assert len(last_df(at)) == 4
    at.selectbox(key="dash_when").select("Custom range").run()
    at.date_input(key="dash_from").set_value(date(2030, 1, 1)).run()
    at.date_input(key="dash_to").set_value(date(2030, 12, 31)).run()  # a future window: nothing was processed then
    assert "No runs match these filters." in alltext(at)
    at.date_input(key="dash_from").set_value(date(2020, 1, 1)).run()
    at.date_input(key="dash_to").set_value(date(2030, 12, 31)).run()
    assert len(last_df(at)) == 4


# ---------------------------------------------------------------- purchase orders


def test_purchase_orders_empty_state_and_controlled_demo_loading(ui_env):
    at = page("orders_page")
    no_exceptions(at)
    assert "No purchase orders yet" in html(at) and "Load synthetic demo purchase orders" in [b.label for b in at.button]
    at.button(key="load_demo").click().run()
    assert any("Added 12 purchase order(s)" in s.value for s in at.success) or len(at.dataframe) > 0
    with session_scope(ui_env) as s:
        assert s.query(PurchaseOrder).count() == 12
    at.button(key="load_demo").click().run()  # idempotent: no duplicates
    assert any("already loaded" in i.value for i in at.info)
    with session_scope(ui_env) as s:
        assert s.query(PurchaseOrder).count() == 12


def test_purchase_orders_table_filters_and_balances(populated):
    at = page("orders_page")
    no_exceptions(at)
    table = at.dataframe[0].value
    assert len(table) == 12
    row = table[table["PO number"] == "PO-2024-1001"].iloc[0]
    assert (row["Total"], row["Approved invoiced"], row["Remaining"], row["Tolerance"], row["Status"]) == (
        "$1,216.00",
        "$1,216.00",
        "$0.00",
        "2%",
        "🟢 Open",
    )
    untouched = table[table["PO number"] == "PO-2024-1005"].iloc[0]
    assert untouched["Total"] == "€7,525.00" and untouched["Approved invoiced"] == "€0.00" and untouched["Remaining"] == "€7,525.00"
    at.selectbox(key="po_status").select("CLOSED").run()
    assert sorted(at.dataframe[0].value["PO number"]) == ["PO-2024-1007", "PO-2024-1009"]
    at.selectbox(key="po_status").select("All").run()
    at.text_input(key="po_q").set_value("globex").run()
    assert list(at.dataframe[0].value["PO number"]) == ["PO-2024-1003"]
    at.text_input(key="po_q").set_value("").run()
    at.selectbox(key="po_cur").select("GBP").run()
    assert set(at.dataframe[0].value["PO number"]) == {"PO-2024-1006", "PO-2024-1009"}


def test_purchase_order_detail_shows_the_approved_invoices(populated):
    at = page("orders_page")
    at.selectbox(key="po_pick").select("PO-2024-1001").run()
    text = html(at)
    assert "Already approved" in text and "$1,216.00" in text and "Maximum still invoiceable" in text and "$24.32" in text
    assert list(at.dataframe[-1].value["Invoice no."]) == ["ACME-INV-20417"]
    at.selectbox(key="po_pick").select("PO-2024-1002").run()
    assert "No invoice has been approved against this purchase order yet." in alltext(at)


# ---------------------------------------------------------------- history


def test_history_empty_state(seeded):
    at = page("history_page")
    no_exceptions(at)
    assert "No processing runs yet" in html(at)


def test_history_lists_filters_and_opens_a_run(populated):
    _, results = populated
    at = page("history_page")
    no_exceptions(at)
    assert len(at.dataframe[0].value) == 4
    assert at.download_button[0].label == "Download CSV report"

    at.multiselect(key="hist_status").set_value(["REVIEW_REQUIRED"]).run()
    assert list(at.dataframe[0].value["File"]) == ["missing.pdf"]
    # the open-run selector follows the filter, and the run's full record is shown
    text = html(at)
    assert "Review required" in text and "What a reviewer must verify" in text and "does not show a purchase order number" in text
    assert "Candidate purchase orders" in text and "never applied automatically" in text
    assert "Approving or rejecting is not available" in alltext(at)

    at.multiselect(key="hist_status").set_value([]).run()
    at.selectbox(key="hist_pick").select(results[0].run_id).run()  # the approved run
    text = html(at)
    assert "Approved by the automated checks" in text and "Invoice ACME-INV-20417" in text
    tabs = [t.label for t in at.tabs]
    assert tabs == ["Financials & PO", "Validation checks", "Invoice details", "Line items", "Execution history", "Source & AI"]
    financial = [d.value for d in at.dataframe if "Remaining after this invoice" in " ".join(map(str, d.value.iloc[:, 0]))]
    assert financial and "$1,240.32" in list(financial[0]["Value"])  # the PO balance calculation is shown
    assert "Passed (" in html(at)

    at.selectbox(key="hist_pick").select(results[3].run_id).run()  # the failed run
    assert any("not a real PDF" in e.value for e in at.markdown) or "Processing failed" in html(at)
    assert "Export or scan the invoice as a PDF" in html(at)


def test_history_search_text(populated):
    at = page("history_page")
    at.text_input(key="hist_q").set_value("std_again").run()
    assert list(at.dataframe[0].value["File"]) == ["std_again.pdf"]
    at.text_input(key="hist_q").set_value("nothing-matches").run()
    assert "No runs match these filters" in alltext(at)


def test_history_deep_link_opens_the_requested_run(populated):
    _, results = populated
    at = AppTest.from_string("import streamlit as st\nfrom invoiceflow.ui import history_page\nhistory_page.render()", default_timeout=60)
    at.session_state["history_open_run"] = results[1].run_id
    at.run()
    no_exceptions(at)
    assert at.selectbox(key="hist_pick").value == results[1].run_id and "Invoice ACME-INV-20433" in html(at)


# ---------------------------------------------------------------- health


def test_health_page_reports_real_status_and_hides_secrets(ui_env, monkeypatch):
    secret_key = "AIzaSyTHISISASECRETKEY0123456789abcdefg"
    monkeypatch.setenv("GEMINI_API_KEY", secret_key)
    monkeypatch.setenv("DATABASE_URL", ui_env.url.render_as_string(hide_password=False))
    at = page("health_page")
    no_exceptions(at)
    text = alltext(at) + "\n" + "\n".join(str(d.value) for d in at.table) + "\n".join(str(j.value) for j in at.json)
    assert "Connected and up to date" in text and "PyMuPDF" in text and "API key is configured" in text
    assert secret_key not in text and "AIzaSy" not in text
    assert "Tesseract" in text  # the OCR card reports the real state either way
    assert any(b.label == "Test Gemini connection" for b in at.button)


def test_health_page_without_key_and_with_unreachable_database(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr("invoiceflow.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://svc:Sup3rSecret@127.0.0.1:1/appdb")
    monkeypatch.setattr("invoiceflow.database.connection.CONNECT_TIMEOUT_SECONDS", 1)
    connection.reset_engine_cache()
    try:
        at = page("health_page")
    finally:
        connection.reset_engine_cache()
    no_exceptions(at)
    text = alltext(at)
    assert "Not reachable" in text and "GEMINI_API_KEY is not set" in text
    assert "Sup3rSecret" not in text and "svc:" not in text
    assert not any(b.label == "Test Gemini connection" for b in at.button)


def test_gemini_connection_test_button(ui_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTHISISASECRETKEY0123456789abcdefg")

    class Fake:
        def check_connection(self):
            return "models/fake-model"

    monkeypatch.setattr("invoiceflow.ui.health_page.GeminiClient.from_settings", lambda settings: Fake())
    at = page("health_page")
    at.button(key="gemini_test").click().run()
    assert any("models/fake-model" in s.value for s in at.success)


# ---------------------------------------------------------------- database down: pages degrade, never crash


def test_pages_explain_a_database_outage_instead_of_crashing(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr("invoiceflow.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://svc:Sup3rSecret@127.0.0.1:1/appdb")
    monkeypatch.setattr("invoiceflow.database.connection.CONNECT_TIMEOUT_SECONDS", 1)
    connection.reset_engine_cache()
    try:
        for module in ("dashboard", "process_page", "orders_page", "history_page"):
            at = page(module)
            no_exceptions(at)
            assert any("database is not available" in e.value for e in at.error), module
            assert "Sup3rSecret" not in alltext(at)
    finally:
        connection.reset_engine_cache()
    assert Invoice and ProcessingRun  # models imported for the fixtures above
