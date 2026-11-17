"""Reusable Streamlit widgets."""
from __future__ import annotations

import streamlit as st

from invoiceflow.config import Settings


def status_badge(label: str, ok: bool, detail: str = "") -> None:
    """One honest status line: green when ready, warning when degraded."""
    text = f"**{label}**" + (f": {detail}" if detail else "")
    (st.success if ok else st.warning)(text)


def config_table(settings: Settings) -> None:
    """Show non-secret configuration."""
    st.table({"setting": list(settings.safe_summary()), "value": list(settings.safe_summary().values())})
