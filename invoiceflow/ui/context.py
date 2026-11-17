"""Shared UI context: settings, database readiness and the pipeline factory.

Kept separate so pages never construct infrastructure themselves (tests substitute ``make_pipeline``).
"""

from __future__ import annotations

import streamlit as st

from invoiceflow.config import ConfigError, Settings, load_settings
from invoiceflow.database.connection import DatabaseUnavailableError, init_db, session_scope
from invoiceflow.services.maintenance import fail_abandoned_runs
from invoiceflow.services.pipeline import InvoicePipeline

PAGES: dict[str, st.Page] = {}  # filled by the app shell so pages can link to each other


def get_settings() -> Settings:
    """Current settings (read every run, so ``.env`` / secrets changes show up without a stale cache)."""
    try:
        return load_settings()
    except ConfigError as exc:
        st.error(f"Invalid configuration: {exc}")
        st.stop()
        raise  # pragma: no cover - st.stop raises


def require_database() -> bool:
    """Make sure the schema is migrated. Shows an explanation and returns False if the database is unusable."""
    try:
        engine = init_db()
        with session_scope(engine) as session:  # close runs that a crash left "in progress" forever
            fail_abandoned_runs(session)
        return True
    except DatabaseUnavailableError as exc:
        st.error(f"**The database is not available.** {exc}")
        st.caption("Pages that need data are disabled until the connection works. See *About & System Health*.")
        return False


def link(page: str, label: str, icon: str | None = None) -> None:
    """A link to another page (silently omitted when running outside the navigation shell, e.g. in tests)."""
    target = PAGES.get(page)
    if target is not None:
        st.page_link(target, label=label, icon=icon)


def go(page: str) -> None:
    target = PAGES.get(page)
    if target is not None:
        st.switch_page(target)


def make_pipeline(settings: Settings) -> InvoicePipeline:
    """The pipeline used by the UI: always the real Gemini provider (tests monkeypatch this function)."""
    return InvoicePipeline(settings)
