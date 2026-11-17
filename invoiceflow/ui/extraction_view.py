"""Extraction tab: upload a PDF, ingest it, run AI extraction, or fall back to manual entry.

Presentation and orchestration only: all parsing, validation and API handling live in services.
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from invoiceflow.ai.invoice_extractor import record_from_manual, run_ai_extraction, unavailable_reason
from invoiceflow.config import Settings
from invoiceflow.database.connection import session_scope
from invoiceflow.database.repositories import save_extraction_record
from invoiceflow.exceptions import IngestionError, OcrUnavailableError
from invoiceflow.extraction.pdf_extractor import extract_document
from invoiceflow.schemas import AiExtractionRecord, ExtractionResult
from invoiceflow.ui.manual_entry import manual_entry_form

logger = logging.getLogger(__name__)
_STATE = "extraction_state"


def _persist(record: AiExtractionRecord) -> None:
    try:
        with session_scope() as session:
            save_extraction_record(session, record)
    except Exception as exc:  # noqa: BLE001 - never lose the on-screen result because storage failed
        logger.exception("Could not save extraction record")
        st.warning(f"The extraction result could not be saved to the database ({type(exc).__name__}).")


def _show_ingestion(result: ExtractionResult) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pages", result.page_count)
    c2.metric("Method", result.method)
    c3.metric("OCR pages", ", ".join(map(str, result.ocr_pages)) or "none")
    c4.metric("Time", f"{result.duration_seconds:.2f}s")
    for w in result.warnings:
        st.warning(w)
    with st.expander("Extracted text"):
        st.text(result.text)


def _show_record(record: AiExtractionRecord) -> None:
    if record.status == "unavailable":
        st.error(f"**AI extraction is unavailable.** {record.error_message}  \nNo data was extracted. Enter the fields manually below.")
    elif record.status == "failed":
        st.error(f"**AI extraction failed.** {record.error_message}  \nNo data was extracted. Enter the fields manually below.")
    elif record.source == "manual":
        st.info("Showing **manually entered** values (not AI-extracted).")
    else:
        st.success(f"AI extraction returned a result ({record.model}, {record.attempts} call(s)). It is unverified: review before relying on it.")
    for w in record.warnings:
        st.warning(w)
    if record.normalized:
        n = record.normalized
        st.json(n.model_dump(mode="json", exclude={"line_items"}), expanded=False)
        if n.line_items:
            st.dataframe(pd.DataFrame([li.model_dump(mode="json") for li in n.line_items]), use_container_width=True)
    if record.evidence:
        with st.expander("Supporting evidence"):
            st.table(pd.DataFrame([e.model_dump() for e in record.evidence]))
    if record.raw_response:
        with st.expander("Raw AI response"):
            st.code(record.raw_response, language="json")


def render(settings: Settings) -> None:
    st.subheader("Extract an invoice")
    reason = unavailable_reason(settings)
    if reason:
        st.warning(f"AI extraction is unavailable: {reason} You can still enter invoice fields manually.")

    upload = st.file_uploader("Invoice PDF", type=["pdf"], help=f"Up to {settings.max_upload_mb} MB and {settings.max_pdf_pages} pages.")
    if upload is None:
        return
    data = upload.getvalue()
    state = st.session_state.get(_STATE)
    if state and state.get("name") != (upload.name, len(data)):
        state = None

    if st.button("Extract", type="primary"):
        try:
            with st.spinner("Reading the document..."):
                ingest = extract_document(data, upload.name, settings, content_type=upload.type)
        except OcrUnavailableError as exc:
            st.error(str(exc))
            return
        except IngestionError as exc:
            st.error(str(exc))
            return
        with st.spinner("Asking Gemini to extract the fields..."):
            record = run_ai_extraction(ingest.text, settings, source_method=ingest.method, file_sha256=ingest.file_sha256)
        _persist(record)
        state = {"name": (upload.name, len(data)), "ingest": ingest, "record": record}
        st.session_state[_STATE] = state

    if not state:
        return
    ingest: ExtractionResult = state["ingest"]
    record: AiExtractionRecord = state["record"]
    _show_ingestion(ingest)
    st.divider()
    _show_record(record)

    st.divider()
    st.markdown("**Correct or enter fields manually**" if record.ai_succeeded else "**Manual entry**")
    entered = manual_entry_form(record.extraction, key="manual")
    if entered is not None:
        manual = record_from_manual(entered, source_text=ingest.text, file_sha256=ingest.file_sha256, based_on=record)
        _persist(manual)
        state["record"] = manual
        st.session_state[_STATE] = state
        st.rerun()
