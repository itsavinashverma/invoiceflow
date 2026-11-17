"""Processing History page: search, filter, export a CSV report, and open any run with its full execution history."""

from __future__ import annotations

from datetime import UTC, datetime

import streamlit as st

from invoiceflow.database.connection import session_scope
from invoiceflow.services import reporting
from invoiceflow.ui import components, context, theme
from invoiceflow.ui.result_view import render_run_view

_REPORT_LIMIT = 5000


def render() -> None:
    context.get_settings()
    theme.page_header("Processing History", "Every run is stored: search it, export it, and open the full record")
    theme.demo_banner()
    if not context.require_database():
        return

    filters = components.history_filters("hist")
    with session_scope() as s:
        rows = reporting.search_runs(s, **filters.as_kwargs(), limit=_REPORT_LIMIT)
        any_runs = bool(rows) or bool(reporting.search_runs(s, limit=1))
    if not any_runs:
        theme.empty_state("No processing runs yet", "Process an invoice and its complete record will appear here.")
        context.link("process", "Process an invoice", "📄")
        return
    if not rows:
        st.info("No runs match these filters. Clear a filter to see more.")
        return

    left, right = st.columns([4, 1.4])
    left.markdown(f"**{len(rows):,}** run(s) match" + (f" (showing the newest {_REPORT_LIMIT:,})" if len(rows) == _REPORT_LIMIT else ""))
    right.download_button(
        "Download CSV report",
        data=reporting.runs_to_csv(rows),
        mime="text/csv",
        width="stretch",
        key="csv",
        file_name=f"invoiceflow_report_{datetime.now(UTC):%Y%m%d_%H%M}.csv",
        help="One row per run, filtered as shown. Amounts are plain decimal numbers.",
    )
    components.runs_table(rows, height=min(480, 38 + 35 * len(rows)))

    st.markdown("#### Open a run")
    shown = rows[:300]
    options = [r.run_id for r in shown]
    wanted = st.session_state.pop("history_open_run", None) or st.query_params.get("run")
    if wanted and wanted not in options:
        with session_scope() as s:
            extra = reporting.search_runs(s, text=wanted, limit=1)
        if extra and extra[0].run_id == wanted:
            options.insert(0, wanted)
            shown = extra + shown
    labels = {r.run_id: f"{r.started_at:%Y-%m-%d %H:%M} · {r.filename} · {theme.decision_text(r.decision_status)}" for r in shown}
    index = options.index(wanted) if wanted in options else 0
    picked = st.selectbox(
        "Run", options, index=index, format_func=lambda rid: labels.get(rid, rid), key="hist_pick", label_visibility="collapsed"
    )
    with session_scope() as s:
        view = reporting.get_run_view(s, picked)
    if view is None:
        st.error("That run could not be found.")
        return
    st.divider()
    render_run_view(view, key="hist")
