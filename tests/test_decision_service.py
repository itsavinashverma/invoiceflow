"""End-to-end decision tests against a real database: the sample POs, the generated invoices, partial
billing, duplicates and concurrency. The decision itself is still the pure engine; these tests prove the
database facts are gathered, applied and saved correctly."""

import threading
from datetime import date
from decimal import Decimal as D
from itertools import count

import pytest

from invoiceflow import constants
from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import Invoice
from invoiceflow.database.seed import seed_purchase_orders
from invoiceflow.schemas import (
    AiExtractionRecord,
    DecisionConfig,
    DecisionResult,
    InvoiceExtraction,
    NormalizedInvoice,
    NormalizedLineItem,
    PurchaseOrderData,
)
from invoiceflow.services import decision_service as svc

APPROVED, REVIEW = constants.DECISION_APPROVED, constants.DECISION_REVIEW_REQUIRED
DUPLICATE, FAILED = constants.DECISION_DUPLICATE_BLOCKED, constants.DECISION_PROCESSING_FAILED
_n = count(1)


@pytest.fixture
def seeded(db_engine):
    with session_scope(db_engine) as s:
        seed_purchase_orders(s)
    return db_engine


def fhash() -> str:
    return f"{next(_n):064x}"


def process(engine, norm, *, file_hash=None, name="inv.pdf", flags=None, config=None) -> DecisionResult:
    """What the pipeline does: record the run (own transaction), then decide+save in one fresh transaction."""
    file_hash = file_hash or fhash()
    with session_scope(engine) as s:
        run_id = repo.create_processing_run(s, name, file_hash).run_id
    with session_scope(engine) as s:
        return svc.decide_and_record(s, run_id, norm, file_hash, config=config, uncertainty_flags=flags)


def line(qty, price, desc="Item"):
    return NormalizedLineItem(description=desc, quantity=D(qty), unit_price=D(price), line_total=D(qty) * D(price))


def inv(number="INV-1", po="PO-2024-1001", vendor="Acme Industrial Supplies Ltd", net="1216.00", tax="85.12", currency="USD", **kw):
    net, tax = D(net), D(tax)
    values = dict(
        invoice_number=number,
        vendor_name=vendor,
        vendor_tax_id=None,
        invoice_date=date(2024, 10, 1),
        purchase_order_number=po,
        currency=currency,
        subtotal=net,
        tax_total=tax,
        grand_total=net + tax,
        line_items=[line("1", net)],
    )
    values.update(kw)
    return NormalizedInvoice(**values)


def to_normalized(g, i) -> NormalizedInvoice:
    """A perfectly extracted copy of a generated invoice (the generator's ground truth)."""
    tax = sum((a for _, _, a in i.tax_amounts), D("0")) if i.taxes else None
    return NormalizedInvoice(
        invoice_number=i.invoice_number,
        vendor_name=i.vendor.name,
        vendor_tax_id=i.vendor.tax_id,
        invoice_date=i.invoice_date,
        purchase_order_number=i.po_number,
        currency=i.currency,
        subtotal=i.subtotal,
        tax_total=tax,
        grand_total=i.total,
        line_items=[
            NormalizedLineItem(
                description=ln.description, item_code=ln.sku, quantity=ln.quantity, unit_price=ln.unit_price, line_total=ln.amount
            )
            for ln in i.lines
        ],
    )


def stored(engine, run_filter=None):
    with session_scope(engine) as s:
        return s.query(Invoice).count()


# ============================================================================ the generated sample invoices


def test_every_sample_invoice_gets_the_expected_decision(seeded, gen_module):
    invoices = gen_module.build_invoices(gen_module.build_purchase_orders())
    outcome = {i.file_name: process(seeded, to_normalized(gen_module, i)) for i in invoices}

    expected_review = {"invoice_missing_po.pdf": "MISSING_PO_REFERENCE", "invoice_wayne_closed_po.pdf": "PO_CLOSED"}
    for name, result in outcome.items():
        if name in expected_review:
            assert result.status == REVIEW and expected_review[name] in result.reason_codes, name
            assert result.matched_po_number is None or name == "invoice_wayne_closed_po.pdf"
        else:
            assert result.status == APPROVED, (name, result.summary)
    assert sum(r.status == APPROVED for r in outcome.values()) == 10

    # the missing-PO Acme invoice only gets a *suggestion*: PO-2024-1001 is Acme's single open PO
    missing = outcome["invoice_missing_po.pdf"]
    assert missing.matched_po_number is None and [s.po_number for s in missing.po_suggestions] == ["PO-2024-1001"]
    assert missing.po_suggestions[0].remaining_balance == D("0.00")  # already fully billed by the standard invoice
    assert any("Suggestion only" in n for n in missing.notes)

    with session_scope(seeded) as s:
        saved = {i.invoice_number: i for i in s.query(Invoice).all()}
        assert len(saved) == 12
        wayne = saved["WP-55012"]
        assert wayne.final_status == REVIEW and wayne.decision_reasons == ["PO_CLOSED"]
        assert wayne.decision_explanation["status"] == "REVIEW_REQUIRED"
        assert saved["ACME-INV-20417"].final_status == APPROVED and saved["ACME-INV-20417"].decision_reasons == []


def test_exact_match_explanation_is_complete_and_persisted(seeded, gen_module):
    i = next(x for x in gen_module.build_invoices(gen_module.build_purchase_orders()) if x.file_name == "invoice_standard_digital.pdf")
    result = process(seeded, to_normalized(gen_module, i))
    assert result.status == APPROVED and result.matched_po_number == "PO-2024-1001"
    assert (result.invoice_total, result.invoice_amount, result.po_amount) == (D("1301.12"), D("1216.00"), D("1216.00"))
    assert (result.already_approved_amount, result.remaining_balance, result.tolerance_percent) == (D("0"), D("1216.00"), D("2.00"))
    assert (result.allowed_overage, result.maximum_allowed_total) == (D("24.32"), D("1240.32"))
    with session_scope(seeded) as s:
        row = s.query(Invoice).one()
        again = DecisionResult.model_validate(row.decision_explanation)
        assert again == result and row.final_status == "APPROVED"


# ============================================================================ partial invoices


def test_partial_invoices_against_the_massive_dynamic_po(seeded):
    # PO-2024-1012: 52,280.00, tolerance 10% => 5,228.00 overage in total.
    partial = lambda n, net: inv(number=n, po="PO-2024-1012", vendor="Massive Dynamic Research Corp", net=net, tax="0.00")  # noqa: E731
    assert process(seeded, partial("M-1", "43000.00")).status == APPROVED
    second = process(seeded, partial("M-2", "9280.00"))
    assert second.status == APPROVED and second.already_approved_amount == D("43000.00") and second.remaining_balance == D("9280.00")
    third = process(seeded, partial("M-3", "5228.00"))  # PO fully used; only the 10% overage is left
    assert third.status == APPROVED and third.remaining_balance == D("0.00") and third.maximum_allowed_total == D("5228.00")
    fourth = process(seeded, partial("M-4", "0.01"))
    assert fourth.status == REVIEW and "AMOUNT_EXCEEDS_PO_LIMIT" in fourth.reason_codes
    assert fourth.already_approved_amount == D("57508.00")  # the cumulative total includes the overage already used


def test_rupee_example_40000_then_60000(db_engine):
    with session_scope(db_engine) as s:
        repo.create_purchase_order_if_missing(
            s,
            PurchaseOrderData(
                po_number="PO-IN-1",
                vendor_name="Bharat Components Pvt Ltd",
                currency="INR",
                total_amount=D("100000.00"),
                tolerance_percent=D("0.00"),
                po_date=date(2024, 9, 1),
            ),
        )
    mk = lambda n, amt: inv(number=n, po="PO-IN-1", vendor="Bharat Components Pvt Ltd", net=amt, tax="0.00", currency="INR")  # noqa: E731
    first = process(db_engine, mk("B-1", "40000.00"))
    second = process(db_engine, mk("B-2", "60000.00"))
    third = process(db_engine, mk("B-3", "1.00"))
    assert (first.status, second.status, third.status) == (APPROVED, APPROVED, REVIEW)
    assert "₹40,000.00" in first.summary and "₹1,00,000.00" in first.summary
    assert second.remaining_balance == D("60000.00") and third.remaining_balance == D("0.00")


# ============================================================================ only APPROVED invoices count


def test_review_duplicate_failed_and_rejected_invoices_never_count_as_approved(seeded):
    big = process(seeded, inv(number="BIG", net="5000.00", tax="0.00"))  # over limit -> REVIEW_REQUIRED
    assert big.status == REVIEW
    dup_source = process(seeded, inv(number="OK-1", net="500.00", tax="0.00"))
    dup = process(seeded, inv(number="OK-1", net="500.00", tax="0.00"))
    assert (dup_source.status, dup.status) == (APPROVED, DUPLICATE)
    with session_scope(seeded) as s:
        run = repo.create_processing_run(s, "x.pdf", fhash())
        svc.record_processing_failure(s, run.run_id, "corrupt")
        rejected = repo.create_processing_run(s, "y.pdf", fhash())
        repo.save_invoice(s, rejected.run_id, inv(number="REJ", net="100.00", tax="0.00"), final_status=constants.DECISION_REJECTED)
    with session_scope(seeded) as s:
        assert [i.invoice_number for i in repo.get_approved_invoices_for_po(s, "PO-2024-1001")] == ["OK-1"]
    next_invoice = process(seeded, inv(number="NEXT", net="716.00", tax="0.00"))
    assert next_invoice.already_approved_amount == D("500.00") and next_invoice.status == APPROVED  # 500 + 716 = 1216


def test_redeciding_a_run_does_not_count_or_duplicate_itself(seeded):
    file_hash = fhash()
    with session_scope(seeded) as s:
        run_id = repo.create_processing_run(s, "a.pdf", file_hash).run_id
    for _ in range(2):
        with session_scope(seeded) as s:
            r = svc.decide_and_record(s, run_id, inv(), file_hash)
        assert r.status == APPROVED and r.already_approved_amount == D("0")
    assert stored(seeded) == 1


# ============================================================================ duplicates


def test_duplicate_invoice_number_blocks_even_with_a_different_file_and_amount(seeded):
    assert process(seeded, inv(number="DUP-1")).status == APPROVED
    again = process(seeded, inv(number=" dup-1 ", net="10.00", tax="0.00"))
    assert again.status == DUPLICATE and "DUPLICATE_INVOICE_NUMBER" in again.reason_codes
    assert "ACME" not in again.summary.upper() or "Acme" in again.summary


def test_duplicate_file_hash_blocks_even_with_a_new_invoice_number(seeded):
    h = fhash()
    assert process(seeded, inv(number="A-1", net="100.00", tax="0.00"), file_hash=h).status == APPROVED
    second = process(seeded, inv(number="A-2", net="100.00", tax="0.00"), file_hash=h)
    assert second.status == DUPLICATE and second.reason_codes == ["DUPLICATE_FILE"]


def test_same_vendor_same_amount_different_numbers_are_both_approved(seeded):
    a = process(seeded, inv(number="S-1", net="300.00", tax="0.00"))
    b = process(seeded, inv(number="S-2", net="300.00", tax="0.00"))
    assert (a.status, b.status) == (APPROVED, APPROVED)


def test_same_number_from_a_different_vendor_is_not_a_duplicate(seeded):
    assert process(seeded, inv(number="X-1")).status == APPROVED
    other = process(seeded, inv(number="X-1", po="PO-2024-1010", vendor="Tyrell Laboratory Equipment Inc", net="10637.50", tax="0.00"))
    assert other.status == APPROVED


def test_retrying_after_a_failed_attempt_is_not_a_duplicate(seeded):
    h = fhash()
    with session_scope(seeded) as s:
        run = repo.create_processing_run(s, "a.pdf", h)
        svc.record_processing_failure(s, run.run_id, "Gemini unavailable")
    assert process(seeded, inv(number="R-1"), file_hash=h).status == APPROVED


def test_tax_id_match_counts_as_same_vendor_for_duplicates(seeded):
    assert process(seeded, inv(number="T-1", vendor_tax_id="US-47-1234567")).status == APPROVED
    again = process(
        seeded, inv(number="T-1", vendor="Acme Industrial (trading name)", vendor_tax_id="us-47-1234567", net="50.00", tax="0.00")
    )
    assert again.status == DUPLICATE


# ============================================================================ PO matching with the database


def test_po_number_lookup_is_normalised_but_not_fuzzy(seeded):
    assert process(seeded, inv(number="L-1", po="po-2024-1001")).status == APPROVED
    near = process(seeded, inv(number="L-2", po="PO 2024-1001"))  # a space inside is not the same number
    assert near.status == REVIEW and near.reason_codes == ["PO_NOT_FOUND"]
    typo = process(seeded, inv(number="L-3", po="PO-2024-1O01"))
    assert typo.status == REVIEW and typo.reason_codes == ["PO_NOT_FOUND"]


def test_vendor_checks_against_the_database(seeded):
    small = dict(net="100.00", tax="0.00")
    assert process(seeded, inv(number="V-1", vendor="ACME INDUSTRIAL SUPPLIES, LIMITED", **small)).status == APPROVED
    wrong = process(seeded, inv(number="V-2", vendor="Globex IT Services LLC", **small))
    assert wrong.status == REVIEW and wrong.reason_codes == ["VENDOR_MISMATCH"]
    llc = process(seeded, inv(number="V-3", vendor="Acme Industrial Supplies LLC", **small))  # PO vendor is "... Ltd"
    assert llc.status == REVIEW and llc.reason_codes == ["VENDOR_MISMATCH"]


def test_closed_po_and_currency_mismatch_with_the_database(seeded):
    closed = process(seeded, inv(number="C-1", po="PO-2024-1007", vendor="Wayne Packaging Co", net="2698.00", tax="0.00"))
    assert closed.status == REVIEW and closed.reason_codes == ["PO_CLOSED"]
    eur = process(seeded, inv(number="C-2", currency="EUR"))
    assert eur.status == REVIEW and eur.reason_codes == ["CURRENCY_MISMATCH"]


def test_missing_po_with_two_open_pos_is_ambiguous_and_nothing_is_guessed(seeded):
    with session_scope(seeded) as s:
        repo.create_purchase_order_if_missing(
            s,
            PurchaseOrderData(
                po_number="PO-2024-2001",
                vendor_name="Acme Industrial Supplies Limited",
                currency="USD",
                total_amount=D("500.00"),
                tolerance_percent=D("1.00"),
                po_date=date(2024, 10, 1),
            ),
        )
    r = process(seeded, inv(number="M-1", po=None))
    assert r.status == REVIEW and {"MISSING_PO_REFERENCE", "AMBIGUOUS_PO_MATCH"} <= set(r.reason_codes)
    assert r.matched_po_number is None and {s.po_number for s in r.po_suggestions} == {"PO-2024-1001", "PO-2024-2001"}
    with session_scope(seeded) as s:
        assert s.query(Invoice).one().po_number is None  # the invoice's own data was not "completed" by a guess


def test_missing_po_single_candidate_is_only_suggested(seeded):
    r = process(seeded, inv(number="M-2", po=None))
    assert r.status == REVIEW and r.reason_codes == ["MISSING_PO_REFERENCE"]
    assert [s.po_number for s in r.po_suggestions] == ["PO-2024-1001"] and r.matched_po_number is None


def test_missing_po_with_unknown_vendor_has_no_suggestions(seeded):
    r = process(seeded, inv(number="M-3", po=None, vendor="Nobody Trading Ltd"))
    assert r.status == REVIEW and r.po_suggestions == [] and any("No purchase order was found" in n for n in r.notes)


def test_a_po_number_matching_two_pos_is_ambiguous(db_engine):
    with session_scope(db_engine) as s:
        for number in ("PO 77", "PO77"):
            repo.create_purchase_order_if_missing(
                s,
                PurchaseOrderData(
                    po_number=number, vendor_name="Acme Ltd", currency="USD", total_amount=D("100.00"), po_date=date(2024, 1, 1)
                ),
            )
    # create_if_missing treats 'PO77' as already present (normalised), so force the second row directly
    from invoiceflow.database.models import PurchaseOrder

    with session_scope(db_engine) as s:
        if s.query(PurchaseOrder).count() == 1:
            s.add(
                PurchaseOrder(
                    po_number="PO77",
                    vendor_name="Acme Ltd",
                    normalized_vendor_name="acme",
                    currency="USD",
                    total_amount=D("100.00"),
                    tolerance_percent=D("0.00"),
                    po_date=date(2024, 1, 1),
                    status="OPEN",
                )
            )
    r = process(db_engine, inv(number="Q-1", po="po77", vendor="Acme Ltd", net="100.00", tax="0.00"))
    assert r.status == REVIEW and "AMBIGUOUS_PO_MATCH" in r.reason_codes and r.matched_po_number is None


# ============================================================================ configuration and failures


def test_po_basis_setting_changes_the_comparison(seeded):
    cfg = DecisionConfig(po_amount_basis="grand_total")
    r = process(seeded, inv(number="G-1", net="1216.00", tax="85.12"), config=cfg)  # 1301.12 > 1240.32
    assert r.status == REVIEW and "AMOUNT_EXCEEDS_PO_LIMIT" in r.reason_codes
    assert process(seeded, inv(number="G-2", net="1216.00", tax="85.12")).status == APPROVED  # default: net of tax


def test_settings_build_the_config(settings):
    from dataclasses import replace

    cfg = svc.config_from_settings(replace(settings, amount_tolerance=D("0.05"), po_amount_basis="grand_total"))
    assert cfg.amount_tolerance == D("0.05") and cfg.po_amount_basis == "grand_total"


def test_processing_failure_is_recorded_and_never_approved(seeded):
    with session_scope(seeded) as s:
        run = repo.create_processing_run(s, "bad.pdf", fhash())
        result = svc.record_processing_failure(s, run.run_id, "The PDF is corrupt or unreadable.")
        run_id = run.run_id
    assert result.status == FAILED
    with session_scope(seeded) as s:
        row = s.query(Invoice).one()
        assert row.final_status == "PROCESSING_FAILED" and row.decision_reasons == ["PROCESSING_FAILED"] and row.grand_total is None
        assert repo.get_run(s, run_id).id == row.processing_run_id


def test_uncertain_extraction_is_forced_to_review(seeded):
    r = process(seeded, inv(number="U-1"), flags=["grand_total 1301.12 does not appear in the document text"])
    assert r.status == REVIEW and r.reason_codes == ["EXTRACTION_UNCERTAIN"]


def _record(*warnings):
    return AiExtractionRecord(source="ai", status="success", extraction=InvoiceExtraction(), warnings=list(warnings))


def test_uncertainty_flags_from_extraction_warnings():
    flags = svc.uncertainty_flags_from_record(
        _record(
            "grand_total 1301.12 does not appear in the document text; verify it.",
            "tax_total 1014.75 does not appear in the document text; verify it.",  # a sum of several tax lines: not a flag
            "invoice_number 'X1' does not appear in the document text; it may be wrong or invented.",
            "Evidence for 'invoice_number' was not found verbatim in the document: 'Invoice No: X1'.",
            "subtotal: 'about 100' is not a valid number; value ignored.",
            "Entered or corrected manually; not produced by AI extraction.",
        ),
        extra=["Page 1: low OCR confidence (41/100)"],
    )
    assert any("grand_total" in f for f in flags) and any("invoice_number" in f for f in flags)
    assert any("Evidence" in f for f in flags) and any("subtotal" in f for f in flags) and any("OCR" in f for f in flags)
    assert not any(f.startswith("tax_total") or "manually" in f for f in flags)
    assert svc.uncertainty_flags_from_record(_record()) == []


# ============================================================================ concurrency


def run_concurrently(engine, invoices):
    """Decide the invoices at the same moment (a barrier lines the threads up). Returns their results."""
    barrier = threading.Barrier(len(invoices))
    results: list = [None] * len(invoices)
    errors: list = []
    ids = []
    for _ in invoices:
        h = fhash()
        with session_scope(engine) as s:
            ids.append((repo.create_processing_run(s, "c.pdf", h).run_id, h))

    def worker(i):
        try:
            barrier.wait(timeout=10)
            with session_scope(engine) as s:
                results[i] = svc.decide_and_record(s, ids[i][0], invoices[i], ids[i][1])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(invoices))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return results


def test_simultaneous_invoices_cannot_overspend_a_po(db_engine):
    with session_scope(db_engine) as s:
        repo.create_purchase_order_if_missing(
            s,
            PurchaseOrderData(
                po_number="PO-C",
                vendor_name="Acme Ltd",
                currency="USD",
                total_amount=D("1000.00"),
                tolerance_percent=D("0.00"),
                po_date=date(2024, 1, 1),
            ),
        )
    invoices = [inv(number=f"C-{i}", po="PO-C", vendor="Acme Ltd", net="400.00", tax="0.00") for i in range(5)]
    results = run_concurrently(db_engine, invoices)
    statuses = sorted(r.status for r in results)
    assert statuses.count(APPROVED) == 2 and statuses.count(REVIEW) == 3  # 400 + 400 fit; a third would make 1,200 > 1,000
    with session_scope(db_engine) as s:
        assert sum(i.grand_total for i in repo.get_approved_invoices_for_po(s, "PO-C")) == D("800.00")


def test_simultaneous_copies_of_one_invoice_are_approved_only_once(db_engine):
    with session_scope(db_engine) as s:
        repo.create_purchase_order_if_missing(
            s,
            PurchaseOrderData(
                po_number="PO-D",
                vendor_name="Acme Ltd",
                currency="USD",
                total_amount=D("10000.00"),
                tolerance_percent=D("0.00"),
                po_date=date(2024, 1, 1),
            ),
        )
    same = inv(number="SAME-1", po="PO-D", vendor="Acme Ltd", net="100.00", tax="0.00")
    results = run_concurrently(db_engine, [same] * 4)
    assert sorted(r.status for r in results) == [APPROVED, DUPLICATE, DUPLICATE, DUPLICATE]


def test_the_decision_lock_really_blocks_a_second_decision(db_engine):
    a_has_lock, release, b_got_lock = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with session_scope(db_engine) as s:
            repo.acquire_decision_lock(s)
            a_has_lock.set()
            release.wait(timeout=20)

    def waiter():
        a_has_lock.wait(timeout=10)
        with session_scope(db_engine) as s:
            repo.acquire_decision_lock(s)
            b_got_lock.set()

    ta, tb = threading.Thread(target=holder), threading.Thread(target=waiter)
    ta.start()
    tb.start()
    assert a_has_lock.wait(timeout=10)
    assert not b_got_lock.wait(timeout=1.0)  # still blocked while A holds the lock
    release.set()
    assert b_got_lock.wait(timeout=20)  # proceeds as soon as A commits
    ta.join(timeout=10)
    tb.join(timeout=10)
