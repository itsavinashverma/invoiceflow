"""Dashboard page. Every number comes from the database; with no records it shows an explanatory empty state."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import session_scope
from invoiceflow.services import reporting
from invoiceflow.ui import components, context, theme


def _amount_card(stats) -> dict:
    totals = stats.invoiced_total_by_currency
    if not totals:
        return dict(label="Invoice amount processed", value="—", tone="primary", note="No extracted amounts yet")
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)  # currencies cannot be added together
    first_cur, first_amount = ordered[0]
    note = "Distinct invoices, by currency" if len(ordered) == 1 else "+ " + " · ".join(theme.fmt_money(a, c) for c, a in ordered[1:])
    return dict(label="Invoice amount processed", value=theme.fmt_money(first_amount, first_cur), tone="primary", note=note)


def render() -> None:
    context.get_settings()
    theme.page_header("Dashboard", "Accounts-payable invoice processing at a glance")
    theme.demo_banner()
    if not context.require_database():
        return

    with session_scope() as s:
        stats = repo.get_dashboard_stats(s)
    by = stats.invoices_by_status
    total = stats.total_invoices

    def share(n: int) -> str:
        return f"{n / total:.0%} of all" if total else "no invoices yet"

    theme.metric_cards(
        [
            dict(
                label="Invoices processed", value=f"{total:,}", tone="primary", note=f"{stats.runs_last_7_days} run(s) in the last 7 days"
            ),
            dict(label="Approved", value=f"{by.get('APPROVED', 0):,}", tone="good", note=share(by.get("APPROVED", 0))),
            dict(
                label="Review required", value=f"{by.get('REVIEW_REQUIRED', 0):,}", tone="warning", note=share(by.get("REVIEW_REQUIRED", 0))
            ),
            dict(
                label="Duplicates blocked",
                value=f"{by.get('DUPLICATE_BLOCKED', 0):,}",
                tone="serious",
                note=share(by.get("DUPLICATE_BLOCKED", 0)),
            ),
            dict(
                label="Processing failures",
                value=f"{by.get('PROCESSING_FAILED', 0):,}",
                tone="critical",
                note=share(by.get("PROCESSING_FAILED", 0)),
            ),
            _amount_card(stats),
        ]
    )

    if total == 0 and stats.total_runs == 0:
        theme.empty_state(
            "No invoices have been processed yet",
            "Upload an invoice PDF to see it extracted, checked against its purchase order and decided. "
            "The demo purchase orders can be loaded from the Purchase Orders page first.",
        )
        c1, c2, _ = st.columns([1, 1, 2])
        with c1:
            context.link("process", "Process your first invoice", "📄")
        with c2:
            context.link("orders", "Load demo purchase orders", "📦")
        return

    left, right = st.columns([5, 7], gap="large")
    with left:
        st.markdown("#### Decision distribution")
        order = components.DECISION_ORDER
        theme.status_bars({k: by.get(k, 0) for k in order}, order)
        with st.expander("View as table"):
            st.dataframe(
                pd.DataFrame([{"Decision": theme.decision_text(k), "Invoices": by.get(k, 0)} for k in order]),
                hide_index=True,
                width="stretch",
            )
        if stats.avg_run_duration_seconds is not None:
            st.caption(f"Average processing time: {stats.avg_run_duration_seconds:.1f}s per run")
    with right:
        st.markdown("#### Recent processing runs")
        with session_scope() as s:
            recent = reporting.search_runs(s, limit=6)
        df = components.runs_dataframe(recent)[["Processed (UTC)", "File", "Total", "Decision"]]
        st.dataframe(df, hide_index=True, width="stretch")

    st.markdown("#### Invoice history")
    filters = components.history_filters("dash")
    with session_scope() as s:
        rows = reporting.search_runs(s, **filters.as_kwargs(), limit=200)
    if not rows:
        st.info("No runs match these filters." if filters.active else "No runs recorded yet.")
    else:
        components.runs_table(rows, height=min(460, 38 + 35 * len(rows)))
        st.caption(f"Showing {len(rows)} most recent matching run(s). Open any run, or export a CSV report, in Processing History.")
    context.link("history", "Open Processing History", "🗂️")
