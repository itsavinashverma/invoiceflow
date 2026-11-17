"""About / System Health: genuine availability checks. Secrets (API key, database password) are never shown."""

from __future__ import annotations

import platform
import sys
from importlib import metadata

import pymupdf
import streamlit as st
from sqlalchemy import func, inspect, select

from invoiceflow import __version__, constants
from invoiceflow.ai.gemini_client import GeminiClient, validate_api_key_format
from invoiceflow.config import storage_warning
from invoiceflow.database import migrations
from invoiceflow.database.connection import DatabaseUnavailableError, check_connection, describe_target, get_engine
from invoiceflow.database.models import Invoice, ProcessingRun, PurchaseOrder
from invoiceflow.exceptions import AiError, GeminiConfigError, OcrUnavailableError
from invoiceflow.extraction.ocr_extractor import ensure_tesseract
from invoiceflow.ui import context, theme


def _card(title: str, ok: bool | None, headline: str, details: list[tuple[str, object]]) -> None:
    tone = "good" if ok else ("warning" if ok is None else "critical")
    icon = "✔" if ok else ("⚠" if ok is None else "✖")
    st.markdown(f'<div class="if-h">{theme.esc(title)}</div>', unsafe_allow_html=True)
    theme.callout(tone, f"{icon} {headline}")
    theme.kv_grid(details)


def database_health(settings) -> tuple[bool, str, list[tuple[str, object]]]:
    """(ok, headline, details); never includes credentials."""
    target = describe_target(settings.database_url)
    try:
        engine = get_engine()
        check_connection(engine)
        with engine.connect() as conn:
            version = migrations.get_schema_version(conn)
            tables = sorted(inspect(conn).get_table_names())
            counts = {
                name: conn.execute(select(func.count()).select_from(model)).scalar()
                for name, model in (("purchase_orders", PurchaseOrder), ("processing_runs", ProcessingRun), ("invoices", Invoice))
                if name in tables
            }
        ok = version == migrations.LATEST_VERSION
        headline = (
            "Connected and up to date" if ok else f"Connected, but the schema is at v{version} (expected v{migrations.LATEST_VERSION})"
        )
        return (
            ok,
            headline,
            [
                ("Target", target),
                ("Engine", engine.dialect.name),
                ("Schema version", f"v{version} of v{migrations.LATEST_VERSION}"),
                ("Purchase orders", counts.get("purchase_orders", "n/a")),
                ("Processing runs", counts.get("processing_runs", "n/a")),
                ("Invoices", counts.get("invoices", "n/a")),
            ],
        )
    except DatabaseUnavailableError as exc:
        return False, "Not reachable", [("Target", target), ("Problem", str(exc))]
    except Exception as exc:
        return False, "Check failed", [("Target", target), ("Problem", f"{type(exc).__name__}")]


def ocr_health(settings) -> tuple[bool, str, list[tuple[str, object]]]:
    try:
        version = ensure_tesseract(settings.tesseract_cmd)
        return (
            True,
            f"Tesseract {version} is available",
            [("Executable", settings.tesseract_cmd or "found on PATH"), ("Language", constants.OCR_LANG)],
        )
    except OcrUnavailableError as exc:
        return (
            False,
            "Not available: scanned PDFs cannot be read",
            [("Looked for", settings.tesseract_cmd or "tesseract on PATH"), ("What to do", str(exc))],
        )


def render() -> None:
    settings = context.get_settings()
    theme.page_header("About & System Health", f"InvoiceFlow v{__version__} · live checks of every dependency")
    theme.demo_banner()

    c1, c2 = st.columns(2, gap="large")
    with c1:
        ok, headline, details = database_health(settings)
        _card("Database", ok, headline, details)
        if warning := storage_warning(settings):
            st.warning(warning)
    with c2:
        try:
            validate_api_key_format(settings.gemini_api_key)
            configured, headline = True, "API key is configured (the key itself is never displayed)"
        except GeminiConfigError as exc:
            configured, headline = False, str(exc)
        _card(
            "Gemini API",
            configured,
            headline,
            [
                ("Model", settings.gemini_model),
                ("Timeout", f"{settings.gemini_timeout_seconds}s"),
                ("Provider in use", "Google Gemini (official google-genai SDK)"),
            ],
        )
        if configured and st.button(
            "Test Gemini connection", key="gemini_test", help="Makes one small real request to Google to verify the key and model."
        ):
            with st.spinner("Contacting Gemini…"):
                try:
                    name = GeminiClient.from_settings(settings).check_connection()
                    st.success(f"Gemini is reachable and the model is available: {name}")
                except AiError as exc:
                    st.error(f"Gemini check failed ({exc.code}): {exc}")

    c3, c4 = st.columns(2, gap="large")
    with c3:
        server_cap = st.get_option("server.maxUploadSize")
        _card(
            "PDF extraction",
            True,
            f"PyMuPDF {pymupdf.__version__} is loaded",
            [
                ("Max upload (app rule)", f"{settings.max_upload_mb} MB"),
                ("Max upload (server cap)", f"{server_cap} MB"),
                ("Max pages", settings.max_pdf_pages),
            ],
        )
        if isinstance(server_cap, int | float) and settings.max_upload_mb > server_cap:
            st.warning(
                f"MAX_UPLOAD_MB is {settings.max_upload_mb} but Streamlit's server rejects uploads above {server_cap} MB "
                "(`server.maxUploadSize` in .streamlit/config.toml). Raise both to allow larger files."
            )
    with c4:
        ok, headline, details = ocr_health(settings)
        _card("OCR", ok, headline, details)

    st.markdown("#### Decision rules in force")
    theme.kv_grid(
        [
            ("Arithmetic rounding tolerance", settings.amount_tolerance),
            ("Amount compared with the PO", "Total excluding tax" if settings.po_amount_basis == "pre_tax" else "Total including tax"),
        ]
    )

    st.markdown("#### About InvoiceFlow")
    st.markdown(
        "InvoiceFlow reads a vendor invoice PDF, extracts its fields, matches it to a purchase order and decides **approve / review / duplicate / failed** "
        "with a full explanation.\n\n"
        "- **The AI only reads.** Gemini extracts and structures the text. Every approval decision comes from deterministic, tested Python rules.\n"
        "- **Uncertain means a human.** Anything unclear, mismatched or unverifiable goes to review. Nothing is ever paid or posted to an accounting system.\n"
        "- **Everything is recorded.** Each run, stage, check and decision is stored; uploaded PDFs themselves are not.\n"
        "- **Synthetic data only.** This is a demonstration product with fictional vendors and purchase orders, and no sign-in, so it offers no approval or payment actions."
    )
    with st.expander("Software versions"):
        pkgs = ["streamlit", "google-genai", "pydantic", "SQLAlchemy", "pymupdf", "pytesseract", "pillow", "psycopg"]
        rows = {"Python": sys.version.split()[0], "Platform": platform.system()}
        for p in pkgs:
            try:
                rows[p] = metadata.version(p)
            except metadata.PackageNotFoundError:
                rows[p] = "not installed"
        st.table(rows)
