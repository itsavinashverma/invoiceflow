"""Reusable widgets shared by several pages: history filters and the runs table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import streamlit as st

from invoiceflow.services.reporting import RunRow
from invoiceflow.ui import theme

DECISION_ORDER = ["APPROVED", "REVIEW_REQUIRED", "DUPLICATE_BLOCKED", "PROCESSING_FAILED"]


@dataclass
class HistoryFilters:
    text: str = ""
    statuses: list[str] | None = None
    date_from: date | None = None
    date_to: date | None = None

    @property
    def active(self) -> bool:
        return bool(self.text.strip() or self.statuses or self.date_from or self.date_to)

    def as_kwargs(self) -> dict:
        return dict(text=self.text or None, statuses=self.statuses or None, date_from=self.date_from, date_to=self.date_to)


DATE_PRESETS = ["All time", "Today", "Last 7 days", "Last 30 days", "Custom range"]


def preset_range(preset: str, today: date) -> tuple[date | None, date | None]:
    """Inclusive UTC day range for a preset (``None`` = unbounded)."""
    return {
        "Today": (today, today),
        "Last 7 days": (today - timedelta(days=6), today),
        "Last 30 days": (today - timedelta(days=29), today),
    }.get(preset, (None, None))


def history_filters(prefix: str) -> HistoryFilters:
    """Search, decision and date filters (they stack on narrow screens)."""
    c1, c2, c3 = st.columns([2.4, 2, 1.4])
    text = c1.text_input("Search", key=f"{prefix}_q", placeholder="Invoice no., vendor, PO, file name or run ID")
    statuses = c2.multiselect(
        "Decision", DECISION_ORDER, key=f"{prefix}_status", format_func=theme.decision_text, placeholder="All decisions"
    )
    preset = c3.selectbox("Processed", DATE_PRESETS, key=f"{prefix}_when")
    today = datetime.now(UTC).date()
    start, end = preset_range(preset, today)
    if preset == "Custom range":
        d1, d2 = st.columns(2)
        start = d1.date_input("From (UTC)", value=today - timedelta(days=30), key=f"{prefix}_from", format="YYYY-MM-DD")
        end = d2.date_input("To (UTC)", value=today, key=f"{prefix}_to", format="YYYY-MM-DD")
        if start and end and end < start:
            st.warning("The end date is before the start date, so no results can match.")
    return HistoryFilters(text=text, statuses=statuses, date_from=start, date_to=end)


def runs_dataframe(rows: list[RunRow]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Processed (UTC)": r.started_at.strftime("%Y-%m-%d %H:%M"),
                "File": r.filename,
                "Invoice no.": r.invoice_number or "—",
                "Vendor": r.vendor_name or "—",
                "PO": r.po_number or "—",
                "Total": theme.fmt_money(r.grand_total, r.currency),
                "Decision": theme.decision_text(r.decision_status),
                "Time": f"{r.duration_seconds:.1f}s" if r.duration_seconds is not None else "…",
            }
            for r in rows
        ]
    )


def runs_table(rows: list[RunRow], *, height: int | None = None) -> None:
    extra = {"height": height} if height is not None else {}  # this Streamlit rejects height=None
    st.dataframe(runs_dataframe(rows), hide_index=True, width="stretch", **extra)
