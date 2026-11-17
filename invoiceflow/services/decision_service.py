"""Glue between the database and the pure decision engine. No UI and no LLM involved.

``decide_and_record`` is the one entry point the pipeline should use for the final decision:

    with session_scope() as session:                      # ONE fresh transaction
        result = decide_and_record(session, run_id, normalized, file_hash)

Inside that transaction it (1) takes the decision lock, (2) reads the PO, its approved invoices and
earlier invoices, (3) runs the pure engine, (4) saves the invoice and the explanation. Because the lock
is held until commit, two simultaneous invoices cannot both be checked against the same stale PO balance,
and two copies of one invoice cannot both be approved.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from invoiceflow.config import Settings
from invoiceflow.database import repositories as repo
from invoiceflow.database.models import Invoice, PurchaseOrder
from invoiceflow.schemas import (
    AiExtractionRecord,
    DecisionConfig,
    DecisionInput,
    DecisionResult,
    NormalizedInvoice,
    PoSnapshot,
    PoSuggestion,
    PriorInvoiceRef,
)
from invoiceflow.services.po_matcher import filter_vendor_candidates
from invoiceflow.services.validation_engine import compute_po_balance, decide, failed_result

logger = logging.getLogger(__name__)

# Audit warnings that mean "a value may have been misread or invented". tax_total is excluded on purpose:
# it is often a sum of several printed tax lines (so it never appears verbatim) and the arithmetic checks
# already verify it against the subtotal and total.
_UNCERTAIN_MARKERS = ("value ignored", "rounded to", "ambiguous", "not a valid", "does not look like")


def config_from_settings(settings: Settings) -> DecisionConfig:
    return DecisionConfig(amount_tolerance=settings.amount_tolerance, po_amount_basis=settings.po_amount_basis)  # type: ignore[arg-type]


def uncertainty_flags_from_record(record: AiExtractionRecord, extra: list[str] | None = None) -> list[str]:
    """Turn extraction warnings into reasons not to trust the data (any flag forces REVIEW_REQUIRED).

    ``extra`` carries ingestion-level doubts such as low OCR confidence.
    """
    flags: list[str] = list(extra or [])
    for warning in record.warnings:
        if warning.startswith(("Entered or corrected manually", "Replaces extraction record")):
            continue
        if warning.startswith("Evidence for"):
            flags.append(warning.rstrip("."))
        elif "does not appear in the document text" in warning:
            if not warning.startswith("tax_total"):
                flags.append(warning.rstrip("."))
        elif any(marker in warning for marker in _UNCERTAIN_MARKERS):
            flags.append(warning.rstrip("."))
    return list(dict.fromkeys(flags))


# ----------------------------------------------------------------------------- row -> pure data


def po_snapshot(row: PurchaseOrder) -> PoSnapshot:
    return PoSnapshot(
        po_number=row.po_number,
        vendor_name=row.vendor_name,
        currency=row.currency,
        total_amount=row.total_amount,
        tolerance_percent=row.tolerance_percent,
        status=row.status,
        po_date=row.po_date,  # type: ignore[arg-type]
    )


def prior_ref(row: Invoice) -> PriorInvoiceRef:
    return PriorInvoiceRef(
        run_id=row.run.run_id,
        invoice_number=row.invoice_number,
        vendor_name=row.vendor_name,
        currency=row.currency,
        grand_total=row.grand_total,
        tax_total=row.tax_total,
        final_status=row.final_status,
        file_hash=row.run.file_hash,
    )


def gather_decision_input(
    session: Session,
    run_id: str,
    normalized: NormalizedInvoice,
    file_hash: str | None,
    config: DecisionConfig,
    uncertainty_flags: list[str] | None = None,
) -> DecisionInput:
    """Read everything the engine needs. Call only after :func:`repo.acquire_decision_lock`."""
    matches = repo.find_purchase_orders_by_number(session, normalized.purchase_order_number)
    po_row = matches[0] if len(matches) == 1 else None
    number_matches = [_suggestion(session, m, config, run_id) for m in matches] if len(matches) > 1 else []

    candidates: list[PoSuggestion] = []
    if po_row is None and len(matches) <= 1:
        shortlist = repo.list_candidate_purchase_orders(session, normalized.vendor_name)
        strict = filter_vendor_candidates(normalized.vendor_name, [po_snapshot(r) for r in shortlist])
        keep = {s.po_number for s in strict}
        candidates = [_suggestion(session, r, config, run_id) for r in shortlist if r.po_number in keep]

    approved = [prior_ref(i) for i in repo.get_approved_invoices_for_po(session, po_row.po_number, exclude_run_id=run_id)] if po_row else []
    same_number = [
        prior_ref(i)
        for i in repo.find_duplicate_invoices(
            session,
            vendor_name=normalized.vendor_name,
            invoice_number=normalized.invoice_number,
            vendor_tax_id=normalized.vendor_tax_id,
            exclude_run_id=run_id,
        )
    ]
    same_file = (
        [prior_ref(i) for i in repo.find_prior_invoices_by_file_hash(session, file_hash, exclude_run_id=run_id)] if file_hash else []
    )
    return DecisionInput(
        invoice=normalized,
        file_hash=file_hash,
        po=po_snapshot(po_row) if po_row else None,
        po_number_matches=number_matches,
        po_candidates=candidates,
        approved_invoices=approved,
        same_number_invoices=same_number,
        same_file_invoices=same_file,
        uncertainty_flags=list(uncertainty_flags or []),
    )


def _suggestion(session: Session, row: PurchaseOrder, config: DecisionConfig, run_id: str) -> PoSuggestion:
    approved = [prior_ref(i) for i in repo.get_approved_invoices_for_po(session, row.po_number, exclude_run_id=run_id)]
    balance = compute_po_balance(po_snapshot(row), approved, config.po_amount_basis)
    return PoSuggestion(
        po_number=row.po_number,
        vendor_name=row.vendor_name,
        status=row.status,  # type: ignore[arg-type]
        currency=row.currency,
        total_amount=row.total_amount,
        remaining_balance=balance.remaining,
    )


# ----------------------------------------------------------------------------- entry points


def evaluate_invoice(
    session: Session,
    run_id: str,
    normalized: NormalizedInvoice,
    file_hash: str | None,
    config: DecisionConfig | None = None,
    uncertainty_flags: list[str] | None = None,
) -> DecisionResult:
    """Lock, gather and decide, without saving. Use :func:`decide_and_record` unless you only need a preview."""
    config = config or DecisionConfig()
    repo.acquire_decision_lock(session)
    return decide(gather_decision_input(session, run_id, normalized, file_hash, config, uncertainty_flags), config)


def decide_and_record(
    session: Session,
    run_id: str,
    normalized: NormalizedInvoice,
    file_hash: str | None,
    *,
    config: DecisionConfig | None = None,
    uncertainty_flags: list[str] | None = None,
    extraction_json: dict | None = None,
) -> DecisionResult:
    """Decide and persist atomically. The caller commits (``session_scope``) after this returns.

    Nothing here pays, posts or notifies anything outside the database.
    """
    result = evaluate_invoice(session, run_id, normalized, file_hash, config, uncertainty_flags)
    repo.save_invoice(
        session,
        run_id,
        normalized,
        final_status=result.status,
        decision_reasons=result.reason_codes,
        extraction_json=extraction_json,
        decision_explanation=result.model_dump(mode="json"),
    )
    logger.info("Run %s decided %s (%s)", run_id[:8], result.status, ", ".join(result.reason_codes) or "no problems")
    return result


def record_processing_failure(
    session: Session,
    run_id: str,
    message: str,
    *,
    normalized: NormalizedInvoice | None = None,
    extraction_json: dict | None = None,
) -> DecisionResult:
    """Persist PROCESSING_FAILED for a document that could not be processed (corrupt PDF, AI outage, ...).

    Whatever was already extracted (``normalized``, ``extraction_json``) is kept on the row so no
    earlier work is lost; the status can never be APPROVED.
    """
    result = failed_result(message, normalized)
    repo.save_invoice(
        session,
        run_id,
        normalized,
        final_status=result.status,
        decision_reasons=result.reason_codes,
        extraction_json=extraction_json,
        decision_explanation=result.model_dump(mode="json"),
    )
    return result
