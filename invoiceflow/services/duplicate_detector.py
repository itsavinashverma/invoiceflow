"""Duplicate detection. Pure functions: no database, UI or LLM.

An invoice is a duplicate when EITHER
* the uploaded file is byte-identical (SHA-256) to one already processed, OR
* the same vendor already has an invoice with the same (normalised) invoice number.

Equal amounts are deliberately NOT a duplicate signal: a vendor can legitimately send two different
invoices for the same amount. A previous attempt that failed with ``PROCESSING_FAILED`` produced no
usable invoice, so it never makes a retry a duplicate.
"""

from __future__ import annotations

from invoiceflow import constants
from invoiceflow.schemas import CheckResult, NormalizedInvoice, PriorInvoiceRef
from invoiceflow.services import checks
from invoiceflow.services.normalizer import normalize_invoice_number
from invoiceflow.utils.money import format_money

CODE_DUPLICATE_FILE = "DUPLICATE_FILE"
CODE_DUPLICATE_NUMBER = "DUPLICATE_INVOICE_NUMBER"

_STATUS_WORDS = {
    constants.DECISION_APPROVED: "approved",
    constants.DECISION_REVIEW_REQUIRED: "awaiting review",
    constants.DECISION_DUPLICATE_BLOCKED: "blocked as a duplicate",
    constants.DECISION_REJECTED: "rejected",
}


def usable_priors(priors: list[PriorInvoiceRef]) -> list[PriorInvoiceRef]:
    """Drop earlier attempts that failed outright: they hold no invoice that could be duplicated."""
    return [p for p in priors if p.final_status != constants.DECISION_PROCESSING_FAILED]


def _describe(prior: PriorInvoiceRef) -> str:
    word = _STATUS_WORDS.get(prior.final_status, prior.final_status.lower())
    amount = f" for {format_money(prior.grand_total, prior.currency)}" if prior.grand_total is not None else ""
    ref = f" (run {prior.run_id[:8]})" if prior.run_id else ""
    return f"invoice {prior.invoice_number or '(no number)'}{amount}, {word}{ref}"


def evaluate_duplicates(
    invoice: NormalizedInvoice,
    same_number: list[PriorInvoiceRef],
    same_file: list[PriorInvoiceRef],
) -> list[CheckResult]:
    """Return the two duplicate checks. ``same_number`` must already be limited to the same vendor."""
    results: list[CheckResult] = []

    file_hits = usable_priors(same_file)
    title = "This exact file has not been processed before"
    if file_hits:
        results.append(
            checks.failed(
                "not_duplicate_file",
                title,
                "This exact file was already processed before: " + "; ".join(_describe(p) for p in file_hits[:3]) + ".",
                CODE_DUPLICATE_FILE,
            )
        )
    else:
        results.append(checks.passed("not_duplicate_file", title, "No earlier upload of this exact file was found."))

    title = "The vendor has not already sent this invoice number"
    wanted = normalize_invoice_number(invoice.invoice_number)
    if wanted is None:
        results.append(checks.skipped("not_duplicate_invoice_number", title, "There is no invoice number to compare."))
    else:
        number_hits = [p for p in usable_priors(same_number) if normalize_invoice_number(p.invoice_number) == wanted]
        if number_hits:
            results.append(
                checks.failed(
                    "not_duplicate_invoice_number",
                    title,
                    f"{invoice.vendor_name or 'This vendor'} has already sent invoice number {invoice.invoice_number}: "
                    + "; ".join(_describe(p) for p in number_hits[:3])
                    + ".",
                    CODE_DUPLICATE_NUMBER,
                )
            )
        else:
            results.append(
                checks.passed(
                    "not_duplicate_invoice_number",
                    title,
                    f"No earlier invoice {invoice.invoice_number} from this vendor was found.",
                )
            )
    return results
