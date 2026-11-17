"""The application shell: page config, theme, sidebar navigation."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from invoiceflow import __version__, constants
from invoiceflow.config import storage_warning
from invoiceflow.ui import access, context, dashboard, health_page, history_page, orders_page, process_page, theme
from invoiceflow.utils.logging_config import setup_logging

LOGO = Path(__file__).parent / "assets" / "logo.svg"


def build_pages() -> dict[str, st.Page]:
    return {
        "dashboard": st.Page(dashboard.render, title="Dashboard", icon=":material/dashboard:", url_path="dashboard", default=True),
        "process": st.Page(process_page.render, title="Process Invoice", icon=":material/upload_file:", url_path="process"),
        "orders": st.Page(orders_page.render, title="Purchase Orders", icon=":material/inventory_2:", url_path="orders"),
        "history": st.Page(history_page.render, title="Processing History", icon=":material/history:", url_path="history"),
        "health": st.Page(health_page.render, title="About / System Health", icon=":material/monitor_heart:", url_path="health"),
    }


def run() -> None:
    st.set_page_config(
        page_title=f"{constants.APP_NAME} · AP automation", page_icon=":material/receipt_long:", layout="wide", initial_sidebar_state="auto"
    )
    theme.inject_css()
    setup_logging()
    settings = context.get_settings()  # stops with a clear message if the configuration is invalid
    access.require_access(settings)  # optional shared access code; stops here until it is entered
    if LOGO.exists():
        st.logo(str(LOGO), size="large")
    pages = build_pages()
    context.PAGES.clear()
    context.PAGES.update(pages)
    nav = st.navigation(list(pages.values()))
    with st.sidebar:
        st.markdown(
            f'<div class="if-demo" style="margin-top:1.2rem"><b>Demo · synthetic data</b><br>AI reads, rules decide. '
            f"Not a production payment-approval system: nothing is ever paid or posted. "
            f"<br><span style='opacity:.7'>v{theme.esc(__version__)}</span></div>",
            unsafe_allow_html=True,
        )
    warning = storage_warning(settings)
    if warning:
        st.warning(warning)
    nav.run()
