"""Application shell. Shows system readiness only; no invoice processing yet."""
from __future__ import annotations

import streamlit as st

from invoiceflow import __version__, constants
from invoiceflow.config import Settings
from invoiceflow.database.connection import check_connection, get_engine, init_db
from invoiceflow.ui import extraction_view
from invoiceflow.ui.components import config_table, status_badge


def render(settings: Settings) -> None:
    st.title(f"🧾 {constants.APP_NAME}")
    st.caption(f"{constants.APP_TAGLINE} · v{__version__}")
    st.info("Extraction only so far: matching, validation and approve/review decisions are not implemented yet.")

    extract_tab, status_tab = st.tabs(["Extract", "System status"])
    with extract_tab:
        extraction_view.render(settings)
    with status_tab:
        _render_status(settings)


def _render_status(settings: Settings) -> None:
    st.subheader("System status")
    status_badge(
        "Gemini API key",
        settings.has_gemini_key,
        "configured" if settings.has_gemini_key else "not set: AI extraction will be unavailable",
    )
    status_badge(
        "OCR (Tesseract)",
        bool(settings.tesseract_cmd),
        settings.tesseract_cmd or "not found: scanned PDFs cannot be read",
    )
    try:
        engine = init_db(get_engine())
        check_connection(engine)
        status_badge("Database", True, f"{engine.dialect.name} ready")
    except Exception as exc:  # noqa: BLE001 - surface any failure visibly
        status_badge("Database", False, f"unavailable ({type(exc).__name__})")

    with st.expander("Configuration (secrets hidden)"):
        config_table(settings)
