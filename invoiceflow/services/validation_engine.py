"""Deterministic financial validation and the final decision. Pure functions: no database, UI or LLM.

Policy (see ``decide``):
    PROCESSING_FAILED  >  DUPLICATE_BLOCKED  >  REVIEW_REQUIRED  >  APPROVED
Every check is always evaluated where its inputs exist, so ALL relevant reasons are reported even when
an earlier one already decides the status. An invoice is APPROVED only if every check passed and a PO
was matched. "Approved" never means "paid": nothing here can trigger a payment or touch accounting.

PO balance (all Decimal):
    remaining_balance     = po_total - already_approved_total
    allowed_overage       = po_total * tolerance_percent / 100          (rounded DOWN to the cent)
    maximum_allowed_total = remaining_balance + allowed_overage
An invoice above ``maximum_allowed_total`` needs review. The compared amount is the invoice total
excluding tax by default (``DecisionConfig.po_amount_basis``); history uses the same basis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from invoiceflow import constants
from invoiceflow.schemas import (
    CheckResult,
    DecisionConfig,
    DecisionInput,
    DecisionResult,
    NormalizedInvoice,
    PoSnapshot,
    PoSuggestion,
    PriorInvoiceRef,
    Reason,
)
from invoiceflow.services import checks
from invoiceflow.services.duplicate_detector import evaluate_duplicates
from invoiceflow.services.po_matcher import evaluate_po_reference
from invoiceflow.utils.money import format_money

ZERO = Decimal("0")
CENT = Decimal("0.01")

CODE_MISSING_INVOICE_NUMBER = "MISSING_INVOICE_NUMBER"
CODE_MISSING_TOTAL = "MISSING_TOTAL"
CODE_NON_POSITIVE_TOTAL = "NON_POSITIVE_TOTAL"
CODE_NO_LINE_ITEMS = "NO_LINE_ITEMS"
CODE_LINE_MISMATCH = "LINE_ARITHMETIC_MISMATCH"
CODE_SUBTOTAL_MISMATCH = "SUBTOTAL_MISMATCH"
CODE_TOTAL_MISMATCH = "TOTAL_MISMATCH"
CODE_UNVERIFIABLE = "ARITHMETIC_UNVERIFIABLE"
CODE_OVER_LIMIT = "AMOUNT_EXCEEDS_PO_LIMIT"
CODE_PRIOR_INCOMPLETE = "PRIOR_INVOICE_DATA_INCOMPLETE"
CODE_UNCERTAIN = "EXTRACTION_UNCERTAIN"
CODE_PROCESSING_FAILED = "PROCESSING_FAILED"

DUPLICATE_CODES = frozenset({"DUPLICATE_FILE", "DUPLICATE_INVOICE_NUMBER"})

HUMAN_ACTIONS = {
    "DUPLICATE_FILE": "Do not pay this again. Check the earlier submission named above; if this is a genuine resubmission, confirm with the vendor first.",
    "DUPLICATE_INVOICE_NUMBER": "Ask the vendor whether this is a re-sent copy or a corrected invoice that reuses the number. Do not approve it as a new invoice.",
    CODE_MISSING_INVOICE_NUMBER: "Find the invoice number on the original document and enter it.",
    "MISSING_PO_REFERENCE": "Find out which purchase order this invoice belongs to (ask the vendor or the requester) and record it.",
    "AMBIGUOUS_PO_MATCH": "Choose the correct purchase order from the candidates listed.",
    "PO_NOT_FOUND": "Check the PO number for typing or scanning errors, or locate or create the purchase order.",
    "MISSING_VENDOR": "Read the vendor name from the original invoice and enter it.",
    "VENDOR_MISMATCH": "Confirm who really issued this invoice and that it belongs to this purchase order before doing anything else.",
    "PO_CLOSED": "Ask the purchase order owner whether the PO should be reopened or the invoice paid some other way.",
    "MISSING_CURRENCY": "Read the currency from the original invoice and enter it.",
    "CURRENCY_MISMATCH": "Ask the vendor to reissue the invoice in the purchase order's currency, or have the PO owner confirm the difference.",
    CODE_OVER_LIMIT: "Ask the PO owner to approve the extra amount (or raise the PO), or ask the vendor for a corrected invoice.",
    CODE_MISSING_TOTAL: "Read the invoice total from the original document and enter it.",
    CODE_NON_POSITIVE_TOTAL: "Check whether this is a credit note or a mistake; credit notes need manual handling.",
    CODE_NO_LINE_ITEMS: "Check the original invoice and enter its line items.",
    CODE_LINE_MISMATCH: "Compare the flagged lines with the original invoice; correct the data or query the vendor.",
    CODE_SUBTOTAL_MISMATCH: "Compare the line amounts with the printed subtotal; correct the data or query the vendor.",
    CODE_TOTAL_MISMATCH: "Compare subtotal, tax, shipping and discount with the printed total; correct the data or query the vendor.",
    CODE_UNVERIFIABLE: "Check the original invoice and fill in the missing amounts so the arithmetic can be verified.",
    CODE_PRIOR_INCOMPLETE: "Fix the earlier approved invoices on this purchase order (missing amount or currency) so the remaining balance can be trusted.",
    CODE_UNCERTAIN: "Compare the extracted values flagged above with the original document and correct any that are wrong.",
    CODE_PROCESSING_FAILED: "Re-upload a valid PDF, or enter the invoice details manually. Nothing was approved.",
}


# ============================================================================= arithmetic


def _fmt(amount: Decimal | None, invoice: NormalizedInvoice) -> str:
    return format_money(amount, invoice.currency)


def check_arithmetic(invoice: NormalizedInvoice, tolerance: Decimal) -> tuple[list[CheckResult], list[str]]:
    """Verify the invoice adds up. Returns ``(results, notes)``. Everything is Decimal."""
    results: list[CheckResult] = []
    notes: list[str] = []
    total = invoice.grand_total

    # --- usable total
    title = "The invoice shows a valid total"
    if total is None:
        results.append(checks.failed("total_present", title, "The invoice total could not be read.", CODE_MISSING_TOTAL))
    elif total <= 0:
        results.append(
            checks.failed(
                "total_present",
                title,
                f"The invoice total is {_fmt(total, invoice)}: zero or negative, which may be a credit note.",
                CODE_NON_POSITIVE_TOTAL,
            )
        )
    else:
        results.append(checks.passed("total_present", title, f"The invoice total is {_fmt(total, invoice)}."))

    # --- line items
    items = invoice.line_items
    title = "The invoice lists its line items"
    if not items:
        results.append(checks.failed("line_items_present", title, "No line items could be read from the invoice.", CODE_NO_LINE_ITEMS))
    else:
        results.append(checks.passed("line_items_present", title, f"{len(items)} line item(s) found."))

    # --- each line: quantity x unit price = line total
    title = "Each line's quantity x price equals its amount"
    unverifiable = [i for i, li in enumerate(items, 1) if li.line_total is None]
    bad_lines: list[str] = []
    not_recomputed = 0
    for i, li in enumerate(items, 1):
        if li.line_total is None:
            continue
        if li.quantity is None or li.unit_price is None:
            not_recomputed += 1
            continue
        expected = li.quantity * li.unit_price
        if abs(expected - li.line_total) > tolerance:
            bad_lines.append(
                f"line {i}: {li.quantity:f} x {_fmt(li.unit_price, invoice)} = {_fmt(expected, invoice)} "
                f"but the invoice shows {_fmt(li.line_total, invoice)}"
            )
    if bad_lines:
        results.append(
            checks.failed("line_arithmetic", title, "Line amounts do not multiply out: " + "; ".join(bad_lines) + ".", CODE_LINE_MISMATCH)
        )
    elif items:
        detail = "Every line's quantity x price matches its amount."
        if not_recomputed:
            detail += f" ({not_recomputed} line(s) lack a quantity or price and could not be recalculated.)"
        results.append(checks.passed("line_arithmetic", title, detail))

    # --- lines add up to the subtotal
    title = "The line amounts add up to the subtotal"
    line_sum: Decimal | None = None
    if items and unverifiable:
        results.append(
            checks.failed(
                "subtotal_matches_lines",
                title,
                f"Line(s) {', '.join(map(str, unverifiable))} have no amount, so the lines cannot be added up.",
                CODE_UNVERIFIABLE,
            )
        )
    elif items:
        line_sum = sum((li.line_total for li in items if li.line_total is not None), ZERO)
        if invoice.subtotal is None:
            notes.append(f"No subtotal was printed; the sum of the lines ({_fmt(line_sum, invoice)}) was used.")
            results.append(
                checks.passed(
                    "subtotal_matches_lines", title, f"The lines add up to {_fmt(line_sum, invoice)} (no printed subtotal to compare)."
                )
            )
        elif abs(line_sum - invoice.subtotal) > tolerance:
            results.append(
                checks.failed(
                    "subtotal_matches_lines",
                    title,
                    f"The lines add up to {_fmt(line_sum, invoice)} but the invoice subtotal is {_fmt(invoice.subtotal, invoice)} "
                    f"(difference {_fmt(abs(line_sum - invoice.subtotal), invoice)}).",
                    CODE_SUBTOTAL_MISMATCH,
                )
            )
        else:
            results.append(
                checks.passed("subtotal_matches_lines", title, f"The lines add up to the subtotal of {_fmt(invoice.subtotal, invoice)}.")
            )

    # --- subtotal + tax + shipping - discount = total
    title = "Subtotal + tax + shipping - discount equals the total"
    base = invoice.subtotal if invoice.subtotal is not None else line_sum
    if total is None:
        results.append(checks.skipped("total_adds_up", title, "There is no total to check."))
    elif base is None:
        results.append(
            checks.failed(
                "total_adds_up",
                title,
                "Neither a subtotal nor complete line amounts are available, so the total cannot be verified.",
                CODE_UNVERIFIABLE,
            )
        )
    else:
        tax = invoice.tax_total or ZERO
        shipping = invoice.shipping_fee or ZERO
        discount = abs(invoice.discount) if invoice.discount is not None else ZERO
        if invoice.discount is not None and invoice.discount < 0:
            notes.append("The discount was shown as a negative number; it was treated as a reduction.")
        expected = base + tax + shipping - discount
        breakdown = (
            f"{_fmt(base, invoice)} + tax {_fmt(tax, invoice)} + shipping {_fmt(shipping, invoice)} "
            f"- discount {_fmt(discount, invoice)} = {_fmt(expected, invoice)}"
        )
        if abs(expected - total) > tolerance:
            results.append(
                checks.failed(
                    "total_adds_up",
                    title,
                    f"{breakdown}, but the invoice total is {_fmt(total, invoice)} (difference {_fmt(abs(expected - total), invoice)}).",
                    CODE_TOTAL_MISMATCH,
                )
            )
        else:
            results.append(checks.passed("total_adds_up", title, f"{breakdown}: matches the invoice total."))
    return results, notes


# ============================================================================= PO balance


def comparison_amount(grand_total: Decimal | None, tax_total: Decimal | None, basis: str) -> Decimal | None:
    """The amount that counts against a PO: the total excluding tax by default (``pre_tax``)."""
    if grand_total is None:
        return None
    if basis == constants.PO_BASIS_GRAND_TOTAL:
        return grand_total
    return grand_total - (tax_total or ZERO)


@dataclass
class PoBalance:
    po_amount: Decimal
    approved_total: Decimal
    remaining: Decimal
    allowed_overage: Decimal
    maximum_allowed: Decimal
    problems: list[str] = field(default_factory=list)  # prior invoices whose data cannot be trusted


def compute_po_balance(po: PoSnapshot, approved: list[PriorInvoiceRef], basis: str) -> PoBalance:
    """remaining = PO total - approved so far; maximum = remaining + total x tolerance% (rounded down)."""
    problems: list[str] = []
    approved_total = ZERO
    for prior in approved:
        label = f"invoice {prior.invoice_number or '(no number)'}"
        if prior.grand_total is None:
            problems.append(f"{label} is approved but has no total")
            continue
        if (prior.currency or "").upper() != po.currency.upper():
            problems.append(f"{label} is approved in {prior.currency or 'an unknown currency'}, not {po.currency}")
            continue
        approved_total += comparison_amount(prior.grand_total, prior.tax_total, basis) or ZERO
    remaining = po.total_amount - approved_total
    overage = (po.total_amount * po.tolerance_percent / Decimal(100)).quantize(CENT, rounding=ROUND_DOWN)
    return PoBalance(po.total_amount, approved_total, remaining, overage, remaining + overage, problems)


def check_po_amount(
    invoice: NormalizedInvoice, po: PoSnapshot, balance: PoBalance, amount: Decimal | None, basis: str
) -> list[CheckResult]:
    cur = po.currency
    results: list[CheckResult] = []
    title = "Earlier approved invoices on this purchase order are reliable"
    if balance.problems:
        results.append(
            checks.failed(
                "prior_invoices_reliable",
                title,
                f"The remaining balance on {po.po_number} cannot be trusted: " + "; ".join(balance.problems) + ".",
                CODE_PRIOR_INCOMPLETE,
            )
        )
    else:
        results.append(checks.passed("prior_invoices_reliable", title, "All earlier approved invoices have a total in the PO currency."))

    title = "The amount fits within the purchase order limit"
    if amount is None:
        results.append(checks.skipped("within_po_limit", title, "There is no invoice total to compare."))
        return results
    what = "excluding tax" if basis == constants.PO_BASIS_PRE_TAX else "including tax"
    working = (
        f"PO total {format_money(balance.po_amount, cur)} - already approved {format_money(balance.approved_total, cur)} "
        f"= remaining {format_money(balance.remaining, cur)}; plus allowed overage {format_money(balance.allowed_overage, cur)} "
        f"({po.tolerance_percent.normalize():f}% tolerance) = maximum {format_money(balance.maximum_allowed, cur)}"
    )
    if amount > balance.maximum_allowed:
        results.append(
            checks.failed(
                "within_po_limit",
                title,
                f"This invoice is {format_money(amount, cur)} ({what}) but at most {format_money(balance.maximum_allowed, cur)} can still be "
                f"invoiced on {po.po_number}: {working}. It is over the limit by {format_money(amount - balance.maximum_allowed, cur)}.",
                CODE_OVER_LIMIT,
            )
        )
    else:
        results.append(
            checks.passed(
                "within_po_limit",
                title,
                f"This invoice is {format_money(amount, cur)} ({what}), within the {format_money(balance.maximum_allowed, cur)} that can still be invoiced: {working}.",
            )
        )
    return results


# ============================================================================= decision


class DecisionInvariantError(RuntimeError):
    """The engine was about to approve something that breaks a safety rule. Never swallowed."""


def failed_result(message: str, invoice: NormalizedInvoice | None = None) -> DecisionResult:
    """PROCESSING_FAILED: the document could not be processed. Never an approval."""
    inv = invoice or NormalizedInvoice()
    return DecisionResult(
        status=constants.DECISION_PROCESSING_FAILED,  # type: ignore[arg-type]
        summary=f"Processing failed: {message} Nothing was approved or paid.",
        invoice_number=inv.invoice_number,
        vendor_name=inv.vendor_name,
        currency=inv.currency,
        reasons=[Reason(code=CODE_PROCESSING_FAILED, message=message)],
        human_actions=[HUMAN_ACTIONS[CODE_PROCESSING_FAILED]],
        basis={"precedence": "PROCESSING_FAILED > DUPLICATE_BLOCKED > REVIEW_REQUIRED > APPROVED"},
    )


class DecisionBuilder:
    """Runs the rules in phases so a pipeline can record each phase as its own stage.

    Order: ``duplicate_checks`` -> ``po_checks`` -> ``financial_checks`` -> ``finish``. Each phase returns
    the checks it produced. :func:`decide` simply runs all four, so both paths give identical results.
    """

    def __init__(self, data: DecisionInput, config: DecisionConfig | None = None) -> None:
        self.data = data
        self.config = config or DecisionConfig()
        self.results: list[CheckResult] = []
        self.skipped: list[CheckResult] = []
        self.notes: list[str] = []
        self.suggestions: list[PoSuggestion] = []
        self.balance: PoBalance | None = None
        inv = data.invoice
        self.amount = comparison_amount(inv.grand_total, inv.tax_total, self.config.po_amount_basis)

    def _add(self, items: list[CheckResult]) -> list[CheckResult]:
        self.results += items
        return items

    # phase 1 ---------------------------------------------------------------
    def duplicate_checks(self) -> list[CheckResult]:
        """Duplicates are evaluated first, so they are always known before anything could approve."""
        inv = self.data.invoice
        out = evaluate_duplicates(inv, self.data.same_number_invoices, self.data.same_file_invoices)
        title = "The invoice shows an invoice number"
        if not inv.invoice_number:
            out.append(checks.failed("invoice_number_present", title, "The invoice number could not be read.", CODE_MISSING_INVOICE_NUMBER))
        else:
            out.append(checks.passed("invoice_number_present", title, f"The invoice number is {inv.invoice_number}."))
        return self._add(out)

    # phase 2 ---------------------------------------------------------------
    def po_checks(self) -> list[CheckResult]:
        """PO reference, vendor, open status, currency (never fuzzy; a missing PO is only ever suggested)."""
        d = self.data
        out, skipped, self.suggestions, notes = evaluate_po_reference(d.invoice, d.po, d.po_candidates, d.po_number_matches)
        self.skipped += skipped
        self.notes += notes
        return self._add(out)

    # phase 3 ---------------------------------------------------------------
    def financial_checks(self) -> list[CheckResult]:
        """Invoice arithmetic, earlier approved invoices, PO balance and tolerance."""
        d, inv, cfg = self.data, self.data.invoice, self.config
        arith, arith_notes = check_arithmetic(inv, cfg.amount_tolerance)
        out = list(arith)
        self.notes += arith_notes
        if cfg.po_amount_basis == constants.PO_BASIS_PRE_TAX and inv.grand_total is not None and inv.tax_total is None:
            self.notes.append(
                "The invoice does not state its tax separately, so the full invoice total was compared with the purchase order."
            )
        limit_title = "The amount fits within the purchase order limit"
        if d.po is not None:
            self.balance = compute_po_balance(d.po, d.approved_invoices, cfg.po_amount_basis)
            if inv.currency and inv.currency.upper() == d.po.currency.upper():
                out += check_po_amount(inv, d.po, self.balance, self.amount, cfg.po_amount_basis)
            else:
                self.skipped.append(
                    checks.skipped(
                        "within_po_limit", limit_title, "The invoice and purchase order currencies are not confirmed to be the same."
                    )
                )
        else:
            self.skipped.append(checks.skipped("within_po_limit", limit_title, "No purchase order is matched to this invoice."))
        return self._add(out)

    # phase 4 ---------------------------------------------------------------
    def finish(self) -> DecisionResult:
        """Check extraction trust, apply precedence, and build the explained result."""
        d, inv, cfg = self.data, self.data.invoice, self.config
        title = "The extracted data is trustworthy"
        if d.uncertainty_flags:
            self._add(
                [
                    checks.failed(
                        "extraction_certain",
                        title,
                        "Some extracted values are uncertain: " + "; ".join(d.uncertainty_flags) + ".",
                        CODE_UNCERTAIN,
                    )
                ]
            )
        else:
            self._add([checks.passed("extraction_certain", title, "No signs of misread or invented values were found.")])

        # A check that could not run (passed=False, no reason code) is reported as skipped, never as a failure or a pass.
        skipped = self.skipped + [r for r in self.results if not r.passed and r.reason_code is None]
        results = [r for r in self.results if r.passed or r.reason_code is not None]
        passed_checks = [r for r in results if r.passed]
        failed_checks = [r for r in results if not r.passed]
        reasons = [Reason(code=r.reason_code or "UNSPECIFIED", message=r.detail) for r in failed_checks]
        codes = [r.code for r in reasons]

        if any(c in DUPLICATE_CODES for c in codes):
            status = constants.DECISION_DUPLICATE_BLOCKED
        elif failed_checks:
            status = constants.DECISION_REVIEW_REQUIRED
        else:
            status = constants.DECISION_APPROVED
        _enforce_invariants(status, failed_checks, d)

        actions: list[str] = []
        for code in codes:
            action = HUMAN_ACTIONS.get(code)
            if action and action not in actions:
                actions.append(action)

        po, balance = d.po, self.balance
        result = DecisionResult(
            status=status,  # type: ignore[arg-type]
            summary="",
            invoice_number=inv.invoice_number,
            vendor_name=inv.vendor_name,
            matched_po_number=po.po_number if po else None,
            currency=inv.currency,
            invoice_total=inv.grand_total,
            invoice_amount=self.amount,
            po_amount=balance.po_amount if balance else None,
            already_approved_amount=balance.approved_total if balance else None,
            remaining_balance=balance.remaining if balance else None,
            tolerance_percent=po.tolerance_percent if po else None,
            allowed_overage=balance.allowed_overage if balance else None,
            maximum_allowed_total=balance.maximum_allowed if balance else None,
            reasons=reasons,
            checks_passed=passed_checks,
            checks_failed=failed_checks,
            checks_skipped=skipped,
            human_actions=actions,
            po_suggestions=self.suggestions,
            notes=self.notes,
            basis={
                "po_amount_basis": cfg.po_amount_basis,
                "amount_tolerance": str(cfg.amount_tolerance),
                "remaining_balance": "PO total - already approved total",
                "allowed_overage": "PO total x tolerance% / 100, rounded down to the cent",
                "maximum_allowed_total": "remaining balance + allowed overage",
                "precedence": "PROCESSING_FAILED > DUPLICATE_BLOCKED > REVIEW_REQUIRED > APPROVED",
            },
        )
        result.summary = build_summary(result)
        return result


def decide(data: DecisionInput, config: DecisionConfig | None = None) -> DecisionResult:
    """Evaluate every rule and return the explained decision. Deterministic: same input, same output."""
    if data.processing_error:
        return failed_result(data.processing_error, data.invoice)
    builder = DecisionBuilder(data, config)
    builder.duplicate_checks()
    builder.po_checks()
    builder.financial_checks()
    return builder.finish()


def _enforce_invariants(status: str, failed_checks: list[CheckResult], data: DecisionInput) -> None:
    """Last line of defence: refuse to return APPROVED unless the basics are really true."""
    if status != constants.DECISION_APPROVED:
        return
    inv = data.invoice
    problems = []
    if failed_checks:
        problems.append("failed checks remain")
    if data.processing_error:
        problems.append("processing error")
    if data.po is None or data.po.status != "OPEN":
        problems.append("no open purchase order")
    if not inv.invoice_number or inv.grand_total is None:
        problems.append("missing invoice number or total")
    if problems:
        raise DecisionInvariantError("Refusing to approve: " + ", ".join(problems))


# ============================================================================= explanation text


def build_summary(r: DecisionResult) -> str:
    who = f"invoice {r.invoice_number}" if r.invoice_number else "this invoice"
    if r.vendor_name:
        who += f" from {r.vendor_name}"
    if r.status == constants.DECISION_APPROVED:
        after = (r.remaining_balance - r.invoice_amount) if (r.remaining_balance is not None and r.invoice_amount is not None) else None
        text = (
            f"Approved: {who} for {format_money(r.invoice_total, r.currency)} passed every check against open purchase order "
            f"{r.matched_po_number}. Its amount of {format_money(r.invoice_amount, r.currency)} is within the "
            f"{format_money(r.maximum_allowed_total, r.currency)} still available "
            f"(PO total {format_money(r.po_amount, r.currency)}, already approved {format_money(r.already_approved_amount, r.currency)})."
        )
        if after is not None:
            text += f" Balance remaining on the PO after this invoice: {format_money(after, r.currency)}."
        return text + " Approval here means the invoice cleared the automated checks; no payment has been made or triggered."
    n = len(r.reasons)
    problems = " ".join(f"({i}) {x.message}" for i, x in enumerate(r.reasons, 1))
    if r.status == constants.DECISION_DUPLICATE_BLOCKED:
        head = f"Blocked as a duplicate: {who} must not be approved."
    else:
        head = f"Review required: {who} was NOT approved because {n} problem{'s were' if n != 1 else ' was'} found."
    return f"{head} {problems} Nothing has been approved or paid."
