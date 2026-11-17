"""Manual entry / correction form: the fallback when AI extraction is unavailable or wrong.

It only collects text and builds an ``InvoiceExtraction``; parsing and validation happen in the same
normalizer used for AI output (``record_from_manual``), so manual data gets no special treatment.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from invoiceflow.schemas import InvoiceExtraction, LineItemExtraction

_SCALAR_FIELDS = [
    ("invoice_number", "Invoice number"),
    ("invoice_date", "Invoice date (YYYY-MM-DD)"),
    ("due_date", "Due date (YYYY-MM-DD)"),
    ("purchase_order_number", "PO number"),
    ("vendor_name", "Vendor name"),
    ("vendor_address", "Vendor address"),
    ("vendor_tax_id", "Vendor tax ID"),
    ("vendor_email", "Vendor email"),
    ("currency", "Currency (ISO code, e.g. USD)"),
    ("subtotal", "Subtotal"),
    ("tax_total", "Tax total"),
    ("shipping_fee", "Shipping fee"),
    ("discount", "Discount"),
    ("grand_total", "Grand total"),
    ("payment_terms", "Payment terms"),
]
_LINE_COLUMNS = ["description", "item_code", "quantity", "unit_price", "line_total"]


def _cell(value: object) -> str | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def lines_to_dataframe(prefill: InvoiceExtraction | None) -> pd.DataFrame:
    rows = [{c: getattr(li, c) or "" for c in _LINE_COLUMNS} for li in (prefill.line_items if prefill else [])]
    return pd.DataFrame(rows or [dict.fromkeys(_LINE_COLUMNS, "")], columns=_LINE_COLUMNS, dtype="object")


def dataframe_to_lines(df: pd.DataFrame) -> list[LineItemExtraction]:
    items = []
    for _, row in df.iterrows():
        values = {c: _cell(row.get(c)) for c in _LINE_COLUMNS}
        if any(values.values()):
            items.append(LineItemExtraction(**values))
    return items


def manual_entry_form(prefill: InvoiceExtraction | None, key: str) -> InvoiceExtraction | None:
    """Render the form; return the entered data once submitted, otherwise ``None``."""
    with st.form(f"manual_{key}"):
        cols = st.columns(2)
        values: dict[str, str] = {}
        for i, (name, label) in enumerate(_SCALAR_FIELDS):
            default = (getattr(prefill, name, None) or "") if prefill else ""
            values[name] = cols[i % 2].text_input(label, value=default, key=f"{key}_{name}")
        st.markdown("**Line items** (add or edit rows; amounts as plain numbers)")
        edited = st.data_editor(lines_to_dataframe(prefill), num_rows="dynamic", key=f"{key}_lines", width="stretch")
        submitted = st.form_submit_button("Use these values")
    if not submitted:
        return None
    return InvoiceExtraction(**{k: _cell(v) for k, v in values.items()}, line_items=dataframe_to_lines(edited))
