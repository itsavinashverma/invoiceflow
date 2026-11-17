"""The invoice result page: one stored run, explained for a finance reader.

Works from ``RunView`` (the database), so a result looks identical straight after processing and when it is
opened later from the history. All document-derived text is escaped or shown through Streamlit's own widgets.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from invoiceflow.schemas import DecisionResult
from invoiceflow.services.reporting import RunView
from invoiceflow.ui import theme

_TONE = {"APPROVED": "good", "REVIEW_REQUIRED": "warning", "DUPLICATE_BLOCKED": "serious", "PROCESSING_FAILED": "critical"}
_HEADLINE = {
    "APPROVED": "Approved by the automated checks",
    "REVIEW_REQUIRED": "Review required: not approved",
    "DUPLICATE_BLOCKED": "Duplicate blocked: must not be paid again",
    "PROCESSING_FAILED": "Processing failed: nothing was approved",
}


def render_run_view(view: RunView, *, key: str = "rv") -> None:
    status = view.run.decision_status
    inv, d = view.invoice, view.decision
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;margin:.2rem 0 .4rem">{theme.decision_badge(status)}'
        f'<span style="font-size:1.25rem;font-weight:650">Invoice {theme.esc(inv.invoice_number) if inv and inv.invoice_number else "(number not read)"}</span>'
        f'<span class="if-sub" style="margin:0">{theme.esc(inv.vendor_name) if inv and inv.vendor_name else "Vendor not read"}</span></div>',
        unsafe_allow_html=True,
    )
    took = f" · {view.run.duration_seconds:.1f}s" if view.run.duration_seconds is not None else ""
    st.caption(f"{view.run.filename} · processed {theme.fmt_time(view.run.started_at)} · run {view.run.run_id[:8]}…{took}")

    theme.callout(_TONE.get(status or "", "neutral"), _HEADLINE.get(status or "", "In progress"), d.summary if d else "")

    if view.error:
        theme.callout("critical", f"{view.error.message}", f"What to do: {view.error.action}")
    _review_panel(view, key)

    tabs = st.tabs(["Financials & PO", "Validation checks", "Invoice details", "Line items", "Execution history", "Source & AI"])
    with tabs[0]:
        _financials(view)
    with tabs[1]:
        _checks(d)
    with tabs[2]:
        _details(view)
    with tabs[3]:
        _line_items(view)
    with tabs[4]:
        _execution(view)
    with tabs[5]:
        _source(view)


# ----------------------------------------------------------------------------- review panel


def _review_panel(view: RunView, key: str) -> None:
    d = view.decision
    status = view.run.decision_status
    if not d or status == "APPROVED":
        return
    if status == "PROCESSING_FAILED" and view.error:
        return  # the error callout already says what to do
    st.markdown("#### What a reviewer must verify" if status == "REVIEW_REQUIRED" else "#### What a person needs to do")
    items = [(c.title, c.detail, "") for c in d.checks_failed]
    theme.check_rows(items, "critical", "✖")
    if d.human_actions:
        st.markdown("**Required actions**")
        for i, action in enumerate(d.human_actions):
            st.checkbox(action, key=f"{key}_act_{i}", help="A personal reminder for this session. It is not saved.")
    if d.po_suggestions:
        st.markdown("**Candidate purchase orders** (suggestions only, never applied automatically)")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "PO": s.po_number,
                        "Vendor": s.vendor_name,
                        "Status": s.status,
                        "Total": theme.fmt_money(s.total_amount, s.currency),
                        "Remaining": theme.fmt_money(s.remaining_balance, s.currency),
                    }
                    for s in d.po_suggestions
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    st.caption("Approving or rejecting is not available in this public demo: there is no sign-in, and no payment can ever be made here.")


# ----------------------------------------------------------------------------- tabs


def _financials(view: RunView) -> None:
    inv, d = view.invoice, view.decision
    cur = (inv.currency if inv else None) or (d.currency if d else None)
    left, right = st.columns(2)
    with left:
        st.markdown('<div class="if-h">Invoice financial summary</div>', unsafe_allow_html=True)
        if inv is None:
            st.info("No financial data was extracted.")
        else:
            rows = [
                ("Subtotal", inv.subtotal),
                ("Tax", inv.tax_total),
                ("Shipping", inv.shipping_fee),
                ("Discount", inv.discount),
                ("Grand total", inv.grand_total),
            ]
            st.dataframe(pd.DataFrame([{"Item": k, "Amount": theme.fmt_money(v, cur)} for k, v in rows]), hide_index=True, width="stretch")
            if inv.currency:
                st.caption(f"Currency: {inv.currency}")
    with right:
        st.markdown('<div class="if-h">Matched purchase order and balance</div>', unsafe_allow_html=True)
        if d is None or d.matched_po_number is None:
            st.info(
                "No purchase order is matched to this invoice."
                + (" See the candidate suggestions above." if d and d.po_suggestions else "")
            )
        else:
            c = d.currency
            after = (d.remaining_balance - d.invoice_amount) if d.remaining_balance is not None and d.invoice_amount is not None else None
            rows = [
                ("Matched PO", d.matched_po_number),
                ("PO total", theme.fmt_money(d.po_amount, c)),
                ("Already approved", theme.fmt_money(d.already_approved_amount, c)),
                ("Remaining before this invoice", theme.fmt_money(d.remaining_balance, c)),
                (f"Allowed tolerance ({d.tolerance_percent}%)", theme.fmt_money(d.allowed_overage, c)),
                ("Maximum that can be invoiced", theme.fmt_money(d.maximum_allowed_total, c)),
                ("This invoice (compared amount)", theme.fmt_money(d.invoice_amount, c)),
                (
                    "Remaining after this invoice" + (" (if approved)" if view.run.decision_status != "APPROVED" else ""),
                    theme.fmt_money(after, c),
                ),
            ]
            st.dataframe(pd.DataFrame(rows, columns=["", "Value"]), hide_index=True, width="stretch")
            basis = d.basis.get("po_amount_basis", "pre_tax")
            st.caption(
                "Remaining = PO total − already approved. Maximum = remaining + tolerance. "
                f"The invoice amount compared is the total {'excluding tax' if basis == 'pre_tax' else 'including tax'}."
            )
    if d and d.notes:
        for note in d.notes:
            theme.callout("info", note)


def _checks(d: DecisionResult | None) -> None:
    if d is None:
        st.info("No checks were recorded for this run.")
        return
    warnings = list(d.notes)
    if warnings:
        st.markdown('<div class="if-h">Warnings and notes</div>', unsafe_allow_html=True)
        theme.check_rows([("Note", w, "") for w in warnings], "warning", "⚠")
    st.markdown(f'<div class="if-h">Failed ({len(d.checks_failed)})</div>', unsafe_allow_html=True)
    if d.checks_failed:
        theme.check_rows([(c.title, c.detail, "") for c in d.checks_failed], "critical", "✖")
    else:
        st.caption("No check failed.")
    st.markdown(f'<div class="if-h">Passed ({len(d.checks_passed)})</div>', unsafe_allow_html=True)
    theme.check_rows([(c.title, c.detail, "") for c in d.checks_passed], "good", "✔")
    if d.checks_skipped:
        st.markdown(f'<div class="if-h">Not run ({len(d.checks_skipped)})</div>', unsafe_allow_html=True)
        theme.check_rows([(c.title, c.detail, "") for c in d.checks_skipped], "neutral", "↷")
    with st.expander("Rules applied"):
        st.json(d.basis, expanded=True)


def _details(view: RunView) -> None:
    inv = view.invoice
    if inv is None:
        st.info("No invoice details were extracted.")
        return
    st.markdown('<div class="if-h">Vendor</div>', unsafe_allow_html=True)
    theme.kv_grid([("Name", inv.vendor_name), ("Address", inv.vendor_address), ("Tax ID", inv.vendor_tax_id), ("Email", inv.vendor_email)])
    st.markdown('<div class="if-h" style="margin-top:1rem">Invoice</div>', unsafe_allow_html=True)
    theme.kv_grid(
        [
            ("Invoice number", inv.invoice_number),
            ("Invoice date", inv.invoice_date),
            ("Due date", inv.due_date),
            ("PO number on invoice", inv.purchase_order_number),
            ("Currency", inv.currency),
            ("Payment terms", inv.payment_terms),
        ]
    )
    if inv.missing_fields:
        st.caption("Not found in the document: " + ", ".join(inv.missing_fields))


def _line_items(view: RunView) -> None:
    inv = view.invoice
    if inv is None or not inv.line_items:
        st.info("No line items were extracted.")
        return
    cur = inv.currency
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "#": i,
                    "Item code": li.item_code or "—",
                    "Description": li.description or "—",
                    "Qty": f"{li.quantity:f}" if li.quantity is not None else "—",
                    "Unit price": theme.fmt_money(li.unit_price, cur),
                    "Line total": theme.fmt_money(li.line_total, cur),
                }
                for i, li in enumerate(inv.line_items, 1)
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(f"{len(inv.line_items)} line item(s).")


def _execution(view: RunView) -> None:
    if not view.events:
        st.info("No execution events are stored for this run.")
        return
    total = sum(e.duration_ms or 0 for e in view.events)
    st.markdown(theme.stage_list_html(view.events), unsafe_allow_html=True)
    st.caption(
        f"Total of stage durations: {total:,} ms · run status: {view.run.run_status}"
        + (f" · {view.overall_error}" if view.overall_error else "")
    )


def _source(view: RunView) -> None:
    ing = view.ingestion
    st.markdown('<div class="if-h">How the document was read</div>', unsafe_allow_html=True)
    if ing:
        st.write(
            f"Text source: **{ing.get('method') or 'not completed'}** · pages: **{ing.get('page_count')}** · "
            f"OCR pages: **{ing.get('ocr_pages') or 'none'}**"
        )
        pages = ing.get("pages") or []
        if pages:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Page": p["page"],
                            "Source": p["method"],
                            "Text characters": p["text_chars"],
                            "OCR confidence": f"{p['ocr_confidence']:.0f}/100" if p.get("ocr_confidence") is not None else "—",
                            "Notes": "; ".join(p.get("warnings") or []),
                        }
                        for p in pages
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
    else:
        st.caption("The document was not read (the run stopped earlier).")
    st.markdown('<div class="if-h" style="margin-top:1rem">AI / data source</div>', unsafe_allow_html=True)
    rec = view.ai_record
    if rec is None:
        st.caption("No extraction record: the run stopped before the AI answered, or it failed.")
        return
    who = "Manual entry" if rec.source == "manual" else f"{rec.model or 'unknown model'}"
    theme.kv_grid([("Source", who), ("Status", rec.status), ("Calls", rec.attempts), ("Recorded", theme.fmt_time(rec.extracted_at))])
    for w in rec.warnings:
        theme.callout("warning", w)
    if rec.error_message:
        theme.callout("critical", rec.error_message)
    if rec.evidence:
        st.markdown("**Evidence quoted from the document**")
        st.dataframe(
            pd.DataFrame(
                [{"Field": e.field, "Quote": e.snippet, "Found in document": "yes" if e.found_in_source else "NO"} for e in rec.evidence]
            ),
            hide_index=True,
            width="stretch",
        )
    if rec.raw_response:
        with st.expander("Raw model response"):
            try:
                st.json(json.loads(rec.raw_response), expanded=False)
            except ValueError:
                st.code(rec.raw_response[:5000])
    st.caption(
        "The AI only reads the document. It never decides: approval is made by deterministic rules, and a model confidence score is never used."
    )
