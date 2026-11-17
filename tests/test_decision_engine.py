"""Unit tests for every decision branch. Pure engine: no database, no network, no LLM."""

from datetime import date
from decimal import Decimal as D

import pytest

from invoiceflow import constants
from invoiceflow.schemas import (
    DecisionConfig,
    DecisionInput,
    NormalizedInvoice,
    NormalizedLineItem,
    PoSnapshot,
    PoSuggestion,
    PriorInvoiceRef,
)
from invoiceflow.services.validation_engine import (
    DecisionInvariantError,
    _enforce_invariants,
    compute_po_balance,
    decide,
    failed_result,
)

APPROVED, REVIEW = constants.DECISION_APPROVED, constants.DECISION_REVIEW_REQUIRED
DUPLICATE, FAILED = constants.DECISION_DUPLICATE_BLOCKED, constants.DECISION_PROCESSING_FAILED


def line(desc="Widget", qty="10", price="100.00", total=None):
    q, p = D(qty), D(price)
    return NormalizedLineItem(description=desc, quantity=q, unit_price=p, line_total=D(total) if total is not None else q * p)


def invoice(**kw) -> NormalizedInvoice:
    """A clean invoice: 1,000.00 net + 7% tax = 1,070.00 USD from Acme, quoting PO-1."""
    values = dict(
        invoice_number="INV-1",
        vendor_name="Acme Industrial Supplies Ltd",
        invoice_date=date(2024, 9, 20),
        purchase_order_number="PO-1",
        currency="USD",
        subtotal=D("1000.00"),
        tax_total=D("70.00"),
        grand_total=D("1070.00"),
        line_items=[line()],
    )
    values.update(kw)
    return NormalizedInvoice(**values)


def po(**kw) -> PoSnapshot:
    values = dict(
        po_number="PO-1",
        vendor_name="Acme Industrial Supplies Ltd",
        currency="USD",
        total_amount=D("1000.00"),
        tolerance_percent=D("2.00"),
        status="OPEN",
    )
    values.update(kw)
    return PoSnapshot(**values)


def prior(number="OLD-1", total="500.00", tax="0.00", status=APPROVED, currency="USD", **kw) -> PriorInvoiceRef:
    return PriorInvoiceRef(
        invoice_number=number,
        vendor_name="Acme Industrial Supplies Ltd",
        currency=currency,
        grand_total=D(total) if total is not None else None,
        tax_total=D(tax),
        final_status=status,
        run_id="a" * 32,
        **kw,
    )


def run(inv=None, **kw):
    data = dict(invoice=inv or invoice(), po=po())
    data.update(kw)
    return decide(DecisionInput(**data))


def codes(result):
    return result.reason_codes


# ============================================================================ the happy path


def test_exact_match_is_approved_with_full_explanation():
    r = run()
    assert r.status == APPROVED and r.reasons == [] and r.checks_failed == [] and r.human_actions == []
    assert r.matched_po_number == "PO-1"
    assert r.invoice_total == D("1070.00") and r.invoice_amount == D("1000.00")  # PO compared net of tax
    assert (r.po_amount, r.already_approved_amount, r.remaining_balance) == (D("1000.00"), D("0"), D("1000.00"))
    assert (r.tolerance_percent, r.allowed_overage, r.maximum_allowed_total) == (D("2.00"), D("20.00"), D("1020.00"))
    passed = {c.check for c in r.checks_passed}
    assert {
        "not_duplicate_file",
        "not_duplicate_invoice_number",
        "invoice_number_present",
        "po_reference_present",
        "po_found",
        "vendor_matches_po",
        "po_is_open",
        "currency_matches_po",
        "total_present",
        "line_items_present",
        "line_arithmetic",
        "subtotal_matches_lines",
        "total_adds_up",
        "prior_invoices_reliable",
        "within_po_limit",
        "extraction_certain",
    } <= passed
    assert r.summary.startswith("Approved:") and "no payment has been made" in r.summary
    assert "$1,000.00" in r.summary and "PO-1" in r.summary


def test_decision_is_deterministic():
    assert run().model_dump(mode="json") == run().model_dump(mode="json")


def test_amount_within_tolerance_is_approved_but_just_over_is_not():
    # remaining 1000.00 + overage 20.00 -> maximum 1020.00 (net of tax)
    at_limit = invoice(subtotal=D("1020.00"), grand_total=D("1091.40"), tax_total=D("71.40"), line_items=[line(price="102.00")])
    assert run(at_limit).status == APPROVED
    over = invoice(
        subtotal=D("1020.01"), grand_total=D("1091.41"), tax_total=D("71.40"), line_items=[line(price="102.001", total="1020.01")]
    )
    r = run(over)
    assert r.status == REVIEW and "AMOUNT_EXCEEDS_PO_LIMIT" in codes(r)
    assert "over the limit by $0.01" in r.reasons[0].message


# ============================================================================ partial invoices (the ₹ example)


def test_partial_invoices_against_one_po():
    inr_po = po(total_amount=D("100000.00"), currency="INR", tolerance_percent=D("0.00"))

    def inv(number, amount):
        return invoice(
            invoice_number=number,
            currency="INR",
            subtotal=D(amount),
            tax_total=D("0.00"),
            grand_total=D(amount),
            line_items=[line(qty="1", price=amount)],
        )

    first = decide(DecisionInput(invoice=inv("INV-A", "40000.00"), po=inr_po))
    assert first.status == APPROVED and first.remaining_balance == D("100000.00")
    assert "₹40,000.00" in first.summary and "₹1,00,000.00" in first.summary  # Indian digit grouping

    approved_first = prior("INV-A", "40000.00", currency="INR")
    second = decide(DecisionInput(invoice=inv("INV-B", "60000.00"), po=inr_po, approved_invoices=[approved_first]))
    assert second.status == APPROVED  # not rejected for differing from the PO total
    assert (second.already_approved_amount, second.remaining_balance) == (D("40000.00"), D("60000.00"))
    assert second.maximum_allowed_total == D("60000.00")

    third = decide(
        DecisionInput(
            invoice=inv("INV-C", "0.01"), po=inr_po, approved_invoices=[approved_first, prior("INV-B", "60000.00", currency="INR")]
        )
    )
    assert third.status == REVIEW and "AMOUNT_EXCEEDS_PO_LIMIT" in codes(third)  # PO fully consumed


def test_cumulative_overage_uses_total_tolerance_not_per_invoice():
    # PO 1000, 2% => 20 overage in total. 600 approved; 400 remaining; max for next = 420.
    ok = decide(
        DecisionInput(
            invoice=invoice(
                invoice_number="B",
                subtotal=D("420.00"),
                tax_total=D("0.00"),
                grand_total=D("420.00"),
                line_items=[line(qty="1", price="420.00")],
            ),
            po=po(),
            approved_invoices=[prior(total="600.00")],
        )
    )
    assert ok.status == APPROVED and ok.maximum_allowed_total == D("420.00")
    too_much = decide(
        DecisionInput(
            invoice=invoice(
                invoice_number="B",
                subtotal=D("420.01"),
                tax_total=D("0.00"),
                grand_total=D("420.01"),
                line_items=[line(qty="1", price="420.01")],
            ),
            po=po(),
            approved_invoices=[prior(total="600.00")],
        )
    )
    assert too_much.status == REVIEW


def test_previous_overage_reduces_what_is_left():
    # 1010 already approved against a 1000 PO (within its 2%): remaining -10, maximum 10.
    bal = compute_po_balance(po(), [prior(total="1010.00")], constants.PO_BASIS_PRE_TAX)
    assert (bal.remaining, bal.allowed_overage, bal.maximum_allowed) == (D("-10.00"), D("20.00"), D("10.00"))


def test_approved_total_is_the_sum_of_the_approved_invoices_net_of_tax():
    r = run(approved_invoices=[prior("A", total="321.00", tax="21.00"), prior("B", total="100.00", tax="0.00")])
    assert r.already_approved_amount == D("400.00") and r.remaining_balance == D("600.00")


def test_tax_basis_can_be_switched_to_grand_total():
    r = decide(DecisionInput(invoice=invoice(), po=po()), DecisionConfig(po_amount_basis="grand_total"))
    assert r.invoice_amount == D("1070.00") and r.status == REVIEW  # 1070 > 1020
    assert "AMOUNT_EXCEEDS_PO_LIMIT" in codes(r)


def test_unstated_tax_uses_full_total_and_says_so():
    r = run(invoice(tax_total=None, grand_total=D("1000.00")))
    assert r.status == APPROVED and r.invoice_amount == D("1000.00")
    assert any("does not state its tax" in n for n in r.notes)


# ============================================================================ review rules


def test_missing_invoice_number():
    r = run(invoice(invoice_number=None))
    assert r.status == REVIEW and codes(r) == ["MISSING_INVOICE_NUMBER"]


def test_missing_po_reference_with_no_candidates():
    r = decide(DecisionInput(invoice=invoice(purchase_order_number=None)))
    assert r.status == REVIEW and "MISSING_PO_REFERENCE" in codes(r) and r.matched_po_number is None
    assert r.po_suggestions == [] and any("No purchase order was found" in n for n in r.notes)
    skipped = {c.check for c in r.checks_skipped}
    assert {"po_found", "vendor_matches_po", "po_is_open", "currency_matches_po", "within_po_limit"} <= skipped
    assert r.remaining_balance is None


def test_missing_po_single_candidate_is_only_a_suggestion():
    cand = PoSuggestion(
        po_number="PO-9",
        vendor_name="Acme Industrial Supplies Ltd",
        status="OPEN",
        currency="USD",
        total_amount=D("1000.00"),
        remaining_balance=D("1000.00"),
    )
    r = decide(DecisionInput(invoice=invoice(purchase_order_number=None), po_candidates=[cand]))
    assert r.status == REVIEW and codes(r) == ["MISSING_PO_REFERENCE"]  # not ambiguous, but never auto-applied
    assert r.matched_po_number is None and r.po_suggestions == [cand]
    assert any("Suggestion only" in n and "PO-9" in n and "NOT been applied" in n for n in r.notes)


def test_ambiguous_po_when_several_candidates():
    cands = [
        PoSuggestion(po_number=n, vendor_name="Acme Industrial Supplies Ltd", status="OPEN", currency="USD", total_amount=D("1000.00"))
        for n in ("PO-8", "PO-9")
    ]
    r = decide(DecisionInput(invoice=invoice(purchase_order_number=None), po_candidates=cands))
    assert r.status == REVIEW and set(codes(r)) == {"MISSING_PO_REFERENCE", "AMBIGUOUS_PO_MATCH"}
    assert r.matched_po_number is None and len(r.po_suggestions) == 2
    assert any("Choose the correct purchase order" in a for a in r.human_actions)


def test_ambiguous_po_number_matching_two_pos():
    matches = [
        PoSuggestion(po_number=n, vendor_name="Acme", status="OPEN", currency="USD", total_amount=D("1.00")) for n in ("PO 1", "PO1")
    ]
    r = decide(DecisionInput(invoice=invoice(purchase_order_number="po-1"), po=None, po_number_matches=matches))
    assert r.status == REVIEW and "AMBIGUOUS_PO_MATCH" in codes(r) and r.matched_po_number is None


def test_closed_candidates_are_only_mentioned():
    cand = PoSuggestion(po_number="PO-7", vendor_name="Acme", status="CLOSED", currency="USD", total_amount=D("5.00"))
    r = decide(DecisionInput(invoice=invoice(purchase_order_number=None), po_candidates=[cand]))
    assert codes(r) == ["MISSING_PO_REFERENCE"] and any("closed" in n for n in r.notes)


def test_po_number_not_found():
    r = decide(DecisionInput(invoice=invoice(purchase_order_number="PO-404"), po=None))
    assert r.status == REVIEW and codes(r) == ["PO_NOT_FOUND"] and "PO-404" in r.reasons[0].message


def test_vendor_mismatch():
    r = run(invoice(vendor_name="Globex IT Services LLC"))
    assert r.status == REVIEW and codes(r) == ["VENDOR_MISMATCH"]
    assert "Globex" in r.reasons[0].message and "Acme" in r.reasons[0].message


def test_same_name_different_company_type_is_a_mismatch():
    r = run(invoice(vendor_name="Acme Industrial Supplies LLC"), po=po(vendor_name="Acme Industrial Supplies Inc"))
    assert r.status == REVIEW and codes(r) == ["VENDOR_MISMATCH"] and "different company type" in r.reasons[0].message


@pytest.mark.parametrize("invoice_vendor", ["ACME INDUSTRIAL SUPPLIES, LIMITED", "Acme Industrial Supplies"])
def test_vendor_spelling_variants_still_match(invoice_vendor):
    r = run(invoice(vendor_name=invoice_vendor))
    assert r.status == APPROVED
    assert invoice_vendor in next(c for c in r.checks_passed if c.check == "vendor_matches_po").detail


def test_missing_vendor_needs_review():
    r = run(invoice(vendor_name=None))
    assert r.status == REVIEW and codes(r) == ["MISSING_VENDOR"]


def test_closed_po():
    r = run(po=po(status="CLOSED"))
    assert r.status == REVIEW and codes(r) == ["PO_CLOSED"]


def test_currency_mismatch():
    r = run(invoice(currency="EUR"))
    assert r.status == REVIEW and codes(r) == ["CURRENCY_MISMATCH"]
    assert "within_po_limit" in {c.check for c in r.checks_skipped}  # amounts in different currencies are never compared


def test_missing_currency():
    r = run(invoice(currency=None))
    assert r.status == REVIEW and codes(r) == ["MISSING_CURRENCY"]


def test_over_tolerance_invoice():
    big = invoice(subtotal=D("2000.00"), tax_total=D("140.00"), grand_total=D("2140.00"), line_items=[line(price="200.00")])
    r = run(big)
    assert r.status == REVIEW and codes(r) == ["AMOUNT_EXCEEDS_PO_LIMIT"]
    msg = r.reasons[0].message
    for fragment in ("$2,000.00", "$1,020.00", "$1,000.00", "2% tolerance", "$980.00"):
        assert fragment in msg


def test_zero_tolerance_po():
    r = run(
        invoice(subtotal=D("1000.01"), tax_total=D("70.00"), grand_total=D("1070.01"), line_items=[line(price="100.001", total="1000.01")]),
        po=po(tolerance_percent=D("0.00")),
    )
    assert r.status == REVIEW and r.allowed_overage == D("0.00")


def test_allowed_overage_rounds_down_to_the_cent():
    bal = compute_po_balance(po(total_amount=D("8680.91"), tolerance_percent=D("1.00")), [], constants.PO_BASIS_PRE_TAX)
    assert bal.allowed_overage == D("86.80")  # 86.8091 -> never rounded up in the vendor's favour


# ============================================================================ arithmetic


def test_arithmetic_mismatch_in_total():
    r = run(invoice(grand_total=D("1170.00")))
    assert r.status == REVIEW and "TOTAL_MISMATCH" in codes(r)
    assert "$1,070.00" in next(x.message for x in r.reasons if x.code == "TOTAL_MISMATCH")


def test_arithmetic_mismatch_in_subtotal():
    r = run(invoice(subtotal=D("900.00"), grand_total=D("970.00")))
    assert r.status == REVIEW and "SUBTOTAL_MISMATCH" in codes(r)


def test_arithmetic_mismatch_in_line():
    r = run(
        invoice(
            line_items=[line(qty="10", price="100.00", total="1000.00"), line(desc="Gadget", qty="3", price="10.00", total="35.00")],
            subtotal=D("1035.00"),
            tax_total=D("0.00"),
            grand_total=D("1035.00"),
        )
    )
    assert "LINE_ARITHMETIC_MISMATCH" in codes(r)
    assert "line 2: 3 x $10.00 = $30.00 but the invoice shows $35.00" in next(
        x.message for x in r.reasons if x.code == "LINE_ARITHMETIC_MISMATCH"
    )


def test_rounding_tolerance_is_configurable():
    off_by_two_cents = invoice(grand_total=D("1070.02"))
    assert decide(DecisionInput(invoice=off_by_two_cents, po=po())).status == REVIEW  # default tolerance 0.01
    r = decide(DecisionInput(invoice=off_by_two_cents, po=po()), DecisionConfig(amount_tolerance=D("0.02")))
    assert r.status == APPROVED
    off_by_one_cent = invoice(grand_total=D("1070.01"))
    assert run(off_by_one_cent).status == APPROVED  # within the 0.01 default


def test_shipping_and_discount_are_part_of_the_arithmetic():
    inv = invoice(shipping_fee=D("25.00"), discount=D("50.00"), grand_total=D("1045.00"))  # 1000 + 70 + 25 - 50
    r = run(inv)
    assert r.status == APPROVED
    assert r.invoice_amount == D("975.00")  # 1045 - 70 tax
    assert run(invoice(shipping_fee=D("25.00"), discount=D("50.00"), grand_total=D("1070.00"))).status == REVIEW


def test_negatively_signed_discount_is_treated_as_a_reduction():
    r = run(invoice(discount=D("-50.00"), grand_total=D("1020.00")))
    assert "TOTAL_MISMATCH" not in codes(r) and any("negative number" in n for n in r.notes)


def test_missing_total_and_non_positive_total():
    assert codes(run(invoice(grand_total=None))) == ["MISSING_TOTAL"]
    credit = run(invoice(subtotal=D("-100.00"), tax_total=D("0.00"), grand_total=D("-100.00"), line_items=[line(qty="1", price="-100.00")]))
    assert credit.status == REVIEW and "NON_POSITIVE_TOTAL" in codes(credit)


def test_no_line_items_needs_review():
    r = run(invoice(line_items=[]))
    assert r.status == REVIEW and codes(r) == ["NO_LINE_ITEMS"]


def test_line_without_amount_cannot_be_verified():
    r = run(invoice(line_items=[line(), NormalizedLineItem(description="mystery", quantity=D("1"))]))
    assert r.status == REVIEW and "ARITHMETIC_UNVERIFIABLE" in codes(r)


def test_missing_subtotal_is_derived_from_lines():
    r = run(invoice(subtotal=None))
    assert r.status == APPROVED and any("No subtotal was printed" in n for n in r.notes)


# ============================================================================ duplicates


def test_duplicate_invoice_number_is_blocked():
    r = run(same_number_invoices=[prior("INV-1", "1070.00", status=APPROVED)])
    assert r.status == DUPLICATE and "DUPLICATE_INVOICE_NUMBER" in codes(r)
    assert "already sent invoice number INV-1" in r.reasons[0].message and "approved" in r.reasons[0].message
    assert r.summary.startswith("Blocked as a duplicate")


def test_duplicate_number_is_normalised():
    r = run(invoice(invoice_number=" inv-1 "), same_number_invoices=[prior("INV-1")])
    assert r.status == DUPLICATE


def test_duplicate_file_hash_is_blocked():
    r = run(same_file_invoices=[prior("OTHER-NUMBER", status=REVIEW)], file_hash="f" * 64)
    assert r.status == DUPLICATE and "DUPLICATE_FILE" in codes(r)


def test_duplicate_wins_over_otherwise_perfect_invoice_and_keeps_every_reason():
    r = run(
        invoice(purchase_order_number="PO-X", vendor_name="Somebody Else"),
        po=None,
        same_number_invoices=[prior("INV-1")],
        same_file_invoices=[prior("INV-1")],
    )
    assert r.status == DUPLICATE
    assert {"DUPLICATE_FILE", "DUPLICATE_INVOICE_NUMBER", "PO_NOT_FOUND"} <= set(codes(r))  # all reasons retained


def test_same_vendor_same_amount_different_number_is_not_a_duplicate():
    other = prior("INV-OTHER", total="1070.00", tax="70.00", status=APPROVED)
    r = run(same_number_invoices=[other])
    assert r.status == APPROVED and "DUPLICATE_INVOICE_NUMBER" not in codes(r)


def test_failed_earlier_attempt_does_not_make_a_retry_a_duplicate():
    failed_before = prior("INV-1", status=FAILED)
    r = run(same_number_invoices=[failed_before], same_file_invoices=[failed_before])
    assert r.status == APPROVED


@pytest.mark.parametrize("prior_status", [APPROVED, REVIEW, DUPLICATE, constants.DECISION_REJECTED])
def test_any_earlier_real_invoice_blocks(prior_status):
    assert run(same_number_invoices=[prior("INV-1", status=prior_status)]).status == DUPLICATE


# ============================================================================ failures, uncertainty, safety


def test_processing_error_is_failed_never_approved():
    r = decide(DecisionInput(invoice=invoice(), po=po(), processing_error="The PDF is corrupt or unreadable."))
    assert r.status == FAILED and codes(r) == ["PROCESSING_FAILED"]
    assert r.summary.startswith("Processing failed") and "Nothing was approved" in r.summary


def test_failed_result_helper_without_invoice():
    r = failed_result("Gemini unavailable")
    assert r.status == FAILED and r.invoice_number is None


def test_uncertain_extraction_cannot_be_approved():
    r = run(uncertainty_flags=["grand_total 1070.00 does not appear in the document text"])
    assert r.status == REVIEW and codes(r) == ["EXTRACTION_UNCERTAIN"]


def test_multiple_problems_are_all_reported():
    r = run(invoice(invoice_number=None, vendor_name="Other Co", currency="EUR", grand_total=D("5000.00")), po=po(status="CLOSED"))
    assert r.status == REVIEW
    assert {"MISSING_INVOICE_NUMBER", "VENDOR_MISMATCH", "PO_CLOSED", "CURRENCY_MISMATCH", "TOTAL_MISMATCH"} <= set(codes(r))
    assert len(r.human_actions) == len(set(r.human_actions)) >= 5  # one clear action each, no repeats


def test_unreliable_prior_invoices_block_approval():
    r = run(approved_invoices=[prior(currency="EUR"), prior("X", total=None)])
    assert r.status == REVIEW and "PRIOR_INVOICE_DATA_INCOMPLETE" in codes(r)
    assert r.already_approved_amount == D("0")


def test_invariant_guard_refuses_to_approve_unsafe_input():
    inv = invoice()
    with pytest.raises(DecisionInvariantError):
        _enforce_invariants(APPROVED, [], DecisionInput(invoice=inv, po=None))
    with pytest.raises(DecisionInvariantError):
        _enforce_invariants(APPROVED, [], DecisionInput(invoice=inv, po=po(status="CLOSED")))
    with pytest.raises(DecisionInvariantError):
        _enforce_invariants(APPROVED, [], DecisionInput(invoice=invoice(invoice_number=None), po=po()))
    with pytest.raises(DecisionInvariantError):
        _enforce_invariants(APPROVED, [], DecisionInput(invoice=inv, po=po(), processing_error="x"))
    _enforce_invariants(APPROVED, [], DecisionInput(invoice=inv, po=po()))  # a safe one passes


def test_explanation_is_readable_by_finance_staff():
    r = run(invoice(grand_total=D("5000.00")))
    text = " ".join([r.summary] + [c.detail for c in r.checks_failed + r.checks_passed] + r.human_actions)
    for jargon in ("Traceback", "None", "Decimal(", "{", "}", "NoneType"):
        assert jargon not in text
    assert all(c.title and c.detail for c in r.checks_passed + r.checks_failed)
    assert r.basis["remaining_balance"] == "PO total - already approved total"


def test_result_is_json_serialisable_with_exact_money():
    dumped = run().model_dump(mode="json")
    assert dumped["maximum_allowed_total"] == "1020.00" and dumped["status"] == "APPROVED"
