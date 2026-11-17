"""Purchase Orders page: all POs with approved-to-date and remaining balances, plus controlled demo data loading."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from invoiceflow.database.connection import session_scope
from invoiceflow.database.seed import SeedDataError, seed_purchase_orders
from invoiceflow.services import decision_service, reporting
from invoiceflow.ui import components, context, theme


def render() -> None:
    settings = context.get_settings()
    theme.page_header("Purchase Orders", "Commitments that invoices are matched against, with what has already been approved")
    theme.demo_banner()
    if not context.require_database():
        return
    config = decision_service.config_from_settings(settings)

    with session_scope() as s:
        everything = reporting.po_overview(s, config)

    _demo_loader(has_pos=bool(everything))
    if not everything:
        theme.empty_state(
            "No purchase orders yet", "Load the synthetic demo purchase orders above to try the matching, balance and tolerance rules."
        )
        return

    open_n = sum(p.status == "OPEN" for p in everything)
    currencies = sorted({p.currency for p in everything})
    theme.metric_cards(
        [
            dict(
                label="Purchase orders",
                value=len(everything),
                tone="primary",
                note=f"{len(currencies)} currenc{'y' if len(currencies) == 1 else 'ies'}",
            ),
            dict(label="Open", value=open_n, tone="good"),
            dict(label="Closed", value=len(everything) - open_n, tone="neutral"),
            dict(label="With approved invoices", value=sum(p.approved_invoice_count > 0 for p in everything), tone="primary"),
        ]
    )

    c1, c2, c3 = st.columns([2.4, 1.2, 1.2])
    search = c1.text_input("Search", key="po_q", placeholder="PO number or vendor")
    status = c2.selectbox("Status", ["All", "OPEN", "CLOSED"], key="po_status")
    currency = c3.selectbox("Currency", ["All", *currencies], key="po_cur")
    with session_scope() as s:
        rows = reporting.po_overview(
            s, config, search=search or None, status=None if status == "All" else status, currency=None if currency == "All" else currency
        )
    if not rows:
        st.info("No purchase orders match these filters.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "PO number": p.po_number,
                    "Vendor": p.vendor_name,
                    "Date": p.po_date.isoformat(),
                    "Total": theme.fmt_money(p.total_amount, p.currency),
                    "Approved invoiced": theme.fmt_money(p.approved_amount, p.currency),
                    "Remaining": theme.fmt_money(p.remaining, p.currency),
                    "Tolerance": f"{p.tolerance_percent.normalize():f}%",
                    "Status": ("🟢 Open" if p.status == "OPEN" else "⚪ Closed"),
                }
                for p in rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    basis = "excluding tax" if config.po_amount_basis == "pre_tax" else "including tax"
    st.caption(f"{len(rows)} of {len(everything)} purchase order(s). Approved amounts count only APPROVED invoices, {basis}.")

    st.markdown("#### Purchase order detail")
    choice = st.selectbox("Select a purchase order", [p.po_number for p in rows], key="po_pick")
    po = next(p for p in rows if p.po_number == choice)
    c = po.currency
    theme.kv_grid(
        [
            ("Vendor", po.vendor_name),
            ("Status", po.status.title()),
            ("PO date", po.po_date.isoformat()),
            ("Currency", c),
            ("PO total", theme.fmt_money(po.total_amount, c)),
            ("Already approved", theme.fmt_money(po.approved_amount, c)),
            ("Remaining", theme.fmt_money(po.remaining, c)),
            (f"Tolerance ({po.tolerance_percent.normalize():f}%)", theme.fmt_money(po.allowed_overage, c)),
            ("Maximum still invoiceable", theme.fmt_money(po.maximum_allowed, c)),
        ]
    )
    for problem in po.problems:
        theme.callout("warning", "Balance cannot be fully trusted", problem)
    with session_scope() as s:
        invoices = reporting.po_approved_invoices(s, po.po_number)
    st.markdown("**Approved invoices against this purchase order**")
    if invoices:
        components.runs_table(invoices)
    else:
        st.caption("No invoice has been approved against this purchase order yet.")


def _demo_loader(*, has_pos: bool) -> None:
    with st.expander("Demo data", expanded=not has_pos):
        st.markdown(
            "Loads the **synthetic** sample purchase orders (fictional vendors in USD, EUR and GBP, with different tolerances, some closed). "
            "It only adds orders that are missing and **never changes or deletes** existing ones, so it is safe to click more than once."
        )
        if st.button("Load synthetic demo purchase orders", key="load_demo"):
            try:
                with session_scope() as s:
                    result = seed_purchase_orders(s)
            except SeedDataError as exc:
                st.error(str(exc))
            else:
                if result.created:
                    st.success(f"Added {len(result.created)} purchase order(s); {len(result.already_present)} were already present.")
                    st.rerun()
                else:
                    st.info(f"Nothing to add: all {len(result.already_present)} demo purchase orders are already loaded.")
