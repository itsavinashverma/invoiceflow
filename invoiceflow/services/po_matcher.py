"""Purchase-order matching. Pure functions: no database, UI or LLM.

Order of work (as required by policy):
1. exact lookup by normalised PO number (done by the caller, result passed in as ``po``)
2. the invoice vendor must be the PO vendor
3. the PO must be OPEN
4. currencies must agree

Without a PO number we only ever *suggest* candidate POs for a human to confirm: a PO reference is
never filled in automatically, and there is no similarity ("fuzzy") matching, so nothing here can by
itself cause an approval.
"""

from __future__ import annotations

from invoiceflow.schemas import CheckResult, NormalizedInvoice, PoSnapshot, PoSuggestion
from invoiceflow.services import checks
from invoiceflow.services.normalizer import compare_vendors, normalize_po_number
from invoiceflow.utils.money import format_money

CODE_MISSING_PO = "MISSING_PO_REFERENCE"
CODE_PO_NOT_FOUND = "PO_NOT_FOUND"
CODE_AMBIGUOUS_PO = "AMBIGUOUS_PO_MATCH"
CODE_MISSING_VENDOR = "MISSING_VENDOR"
CODE_VENDOR_MISMATCH = "VENDOR_MISMATCH"
CODE_PO_CLOSED = "PO_CLOSED"
CODE_MISSING_CURRENCY = "MISSING_CURRENCY"
CODE_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


def filter_vendor_candidates(vendor_name: str | None, pos: list[PoSnapshot]) -> list[PoSnapshot]:
    """POs whose vendor is the same company as ``vendor_name`` (strict comparison, see ``compare_vendors``)."""
    return [p for p in pos if compare_vendors(vendor_name, p.vendor_name).matches]


def _describe_candidate(c: PoSuggestion) -> str:
    return f"{c.po_number} ({c.status.lower()}, {format_money(c.total_amount, c.currency)})"


def evaluate_po_reference(
    invoice: NormalizedInvoice,
    po: PoSnapshot | None,
    candidates: list[PoSuggestion],
    number_matches: list[PoSuggestion] | None = None,
) -> tuple[list[CheckResult], list[CheckResult], list[PoSuggestion], list[str]]:
    """Run the PO-related checks.

    Returns ``(results, skipped, suggestions, notes)``. ``suggestions`` are shown to the reviewer and
    never applied. ``po`` is the PO found by exact normalised-number lookup (or ``None``);
    ``number_matches`` lists *every* PO the normalised number matched, so that more than one is
    reported as ambiguous instead of picking one.
    """
    results: list[CheckResult] = []
    skipped: list[CheckResult] = []
    notes: list[str] = []
    suggestions: list[PoSuggestion] = []
    reference = invoice.purchase_order_number
    has_reference = normalize_po_number(reference) is not None

    # --- is there a PO reference, and does it exist?
    title = "The invoice quotes a purchase order number"
    if not has_reference:
        results.append(
            checks.failed(
                "po_reference_present",
                title,
                "The invoice does not show a purchase order number.",
                CODE_MISSING_PO,
            )
        )
        open_candidates = [c for c in candidates if c.status == "OPEN"]
        closed_candidates = [c for c in candidates if c.status == "CLOSED"]
        suggestions = open_candidates or closed_candidates
        if len(open_candidates) > 1:
            results.append(
                checks.failed(
                    "po_match_unambiguous",
                    "Only one purchase order could apply",
                    f"{len(open_candidates)} open purchase orders exist for this vendor, so the right one cannot be "
                    "chosen automatically: " + ", ".join(_describe_candidate(c) for c in open_candidates) + ".",
                    CODE_AMBIGUOUS_PO,
                )
            )
        elif len(open_candidates) == 1:
            notes.append(
                f"Suggestion only: purchase order {_describe_candidate(open_candidates[0])} is the only open PO for "
                "this vendor. It has NOT been applied; a person must confirm it."
            )
        elif closed_candidates:
            notes.append(
                "Only closed purchase orders exist for this vendor: " + ", ".join(_describe_candidate(c) for c in closed_candidates) + "."
            )
        else:
            notes.append("No purchase order was found for this vendor.")
        for check, title in (
            ("po_found", "The purchase order exists"),
            ("vendor_matches_po", "The vendor matches the purchase order"),
            ("po_is_open", "The purchase order is open"),
            ("currency_matches_po", "The currency matches the purchase order"),
        ):
            skipped.append(checks.skipped(check, title, "No purchase order is matched to this invoice."))
        return results, skipped, suggestions, notes

    results.append(checks.passed("po_reference_present", title, f"The invoice quotes purchase order {reference}."))

    if po is None and number_matches and len(number_matches) > 1:
        results.append(
            checks.failed(
                "po_match_unambiguous",
                "Only one purchase order could apply",
                f"The number {reference} matches {len(number_matches)} different purchase orders, so the right one cannot be "
                "chosen automatically: " + ", ".join(_describe_candidate(c) for c in number_matches) + ".",
                CODE_AMBIGUOUS_PO,
            )
        )
        for check, title in (
            ("po_found", "The purchase order exists"),
            ("vendor_matches_po", "The vendor matches the purchase order"),
            ("po_is_open", "The purchase order is open"),
            ("currency_matches_po", "The currency matches the purchase order"),
        ):
            skipped.append(checks.skipped(check, title, "The purchase order number is ambiguous."))
        return results, skipped, list(number_matches), notes

    title = "The purchase order exists"
    if po is None:
        results.append(
            checks.failed(
                "po_found",
                title,
                f"Purchase order {reference} was not found in the system.",
                CODE_PO_NOT_FOUND,
            )
        )
        suggestions = [c for c in candidates if c.status == "OPEN"]
        if suggestions:
            notes.append(
                "Open purchase orders for this vendor (for the reviewer's reference only): "
                + ", ".join(_describe_candidate(c) for c in suggestions)
                + "."
            )
        for check, title in (
            ("vendor_matches_po", "The vendor matches the purchase order"),
            ("po_is_open", "The purchase order is open"),
            ("currency_matches_po", "The currency matches the purchase order"),
        ):
            skipped.append(checks.skipped(check, title, f"Purchase order {reference} was not found."))
        return results, skipped, suggestions, notes
    results.append(checks.passed("po_found", title, f"Purchase order {po.po_number} was found."))

    # --- vendor
    title = "The vendor matches the purchase order"
    vendor = compare_vendors(invoice.vendor_name, po.vendor_name)
    if vendor.kind == "missing":
        results.append(
            checks.failed(
                "vendor_matches_po",
                title,
                f"The invoice vendor could not be read, so it cannot be checked against {po.vendor_name} on {po.po_number}.",
                CODE_MISSING_VENDOR,
            )
        )
    elif not vendor.matches:
        extra = " (same name but a different company type)" if vendor.kind == "different_legal_form" else ""
        results.append(
            checks.failed(
                "vendor_matches_po",
                title,
                f"The invoice is from '{invoice.vendor_name}' but {po.po_number} was issued to '{po.vendor_name}'{extra}.",
                CODE_VENDOR_MISMATCH,
            )
        )
    else:
        detail = f"'{invoice.vendor_name}' is the vendor on {po.po_number}."
        if vendor.kind == "legal_form_omitted":
            detail = (
                f"'{invoice.vendor_name}' matches '{po.vendor_name}' on {po.po_number} "
                "(one side omits the legal ending such as Ltd or Inc)."
            )
        results.append(checks.passed("vendor_matches_po", title, detail))

    # --- open?
    title = "The purchase order is open"
    if po.status != "OPEN":
        results.append(checks.failed("po_is_open", title, f"Purchase order {po.po_number} is closed.", CODE_PO_CLOSED))
    else:
        results.append(checks.passed("po_is_open", title, f"Purchase order {po.po_number} is open."))

    # --- currency
    title = "The currency matches the purchase order"
    if not invoice.currency:
        results.append(
            checks.failed(
                "currency_matches_po",
                title,
                f"The invoice currency could not be read, so it cannot be checked against {po.currency} on {po.po_number}.",
                CODE_MISSING_CURRENCY,
            )
        )
    elif invoice.currency.upper() != po.currency.upper():
        results.append(
            checks.failed(
                "currency_matches_po",
                title,
                f"The invoice is in {invoice.currency} but {po.po_number} is in {po.currency}.",
                CODE_CURRENCY_MISMATCH,
            )
        )
    else:
        results.append(checks.passed("currency_matches_po", title, f"Both are in {po.currency}."))
    return results, skipped, suggestions, notes
