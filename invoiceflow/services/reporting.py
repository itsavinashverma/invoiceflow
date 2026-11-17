"""Read models for the user interface: everything a page shows comes through here, from the database.

No rule or decision is made in this module (those live in the engine); it only queries, assembles and
formats stored records. The UI therefore holds no business logic and shows no data that is not in the
database.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from invoiceflow import constants
from invoiceflow.database import repositories as repo
from invoiceflow.database.models import Invoice, ProcessingRun, PurchaseOrder
from invoiceflow.schemas import (
    AiExtractionRecord,
    DecisionConfig,
    DecisionResult,
    NormalizedInvoice,
    PipelineError,
    PriorInvoiceRef,
    StageEvent,
)
from invoiceflow.services.decision_service import po_snapshot, prior_ref
from invoiceflow.services.normalizer import normalize_po_number, normalize_vendor_name
from invoiceflow.services.pipeline import ACTIONS
from invoiceflow.services.validation_engine import compute_po_balance

_TITLES = dict(constants.PIPELINE_STAGES)
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


# ============================================================================= runs


class RunRow(BaseModel):
    """One processing run with its invoice outcome: a row in the history tables and the CSV report."""

    run_id: str
    filename: str
    file_hash: str
    started_at: datetime
    completed_at: datetime | None = None
    run_status: str
    duration_seconds: float | None = None
    decision_status: str | None = None  # None while the run is still in progress
    invoice_number: str | None = None
    vendor_name: str | None = None
    po_number: str | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    grand_total: Decimal | None = None
    reasons: list[str] = Field(default_factory=list)


def _row(run: ProcessingRun, inv: Invoice | None) -> RunRow:
    duration = (run.completed_at - run.started_at).total_seconds() if run.completed_at else None
    return RunRow(
        run_id=run.run_id,
        filename=run.original_filename,
        file_hash=run.file_hash,
        started_at=run.started_at,
        completed_at=run.completed_at,
        run_status=run.run_status,
        duration_seconds=duration,
        decision_status=inv.final_status if inv else None,
        invoice_number=inv.invoice_number if inv else None,
        vendor_name=inv.vendor_name if inv else None,
        po_number=inv.po_number if inv else None,
        currency=inv.currency if inv else None,
        subtotal=inv.subtotal if inv else None,
        tax_total=inv.tax_total if inv else None,
        grand_total=inv.grand_total if inv else None,
        reasons=list(inv.decision_reasons or []) if inv else [],
    )


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def search_runs(
    session: Session,
    *,
    text: str | None = None,
    statuses: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[RunRow]:
    """Processing runs, newest first. ``text`` matches filename, run id, invoice number, vendor or PO number
    (case-insensitive substring, wildcards escaped). ``date_from``/``date_to`` are inclusive UTC days of
    the run's start time. ``statuses`` are decision statuses."""
    stmt = select(ProcessingRun, Invoice).outerjoin(Invoice, Invoice.processing_run_id == ProcessingRun.id)
    if text and text.strip():
        like = f"%{repo._like(text)}%"
        stmt = stmt.where(
            or_(
                *(
                    func.lower(col).like(like, escape="\\")
                    for col in (
                        ProcessingRun.original_filename,
                        ProcessingRun.run_id,
                        Invoice.invoice_number,
                        Invoice.vendor_name,
                        Invoice.po_number,
                    )
                )
            )
        )
    if statuses:
        stmt = stmt.where(Invoice.final_status.in_(list(statuses)))
    if date_from:
        stmt = stmt.where(ProcessingRun.started_at >= _day_start(date_from))
    if date_to:
        stmt = stmt.where(ProcessingRun.started_at < _day_start(date_to) + timedelta(days=1))
    stmt = stmt.order_by(ProcessingRun.started_at.desc(), ProcessingRun.id.desc()).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [_row(run, inv) for run, inv in session.execute(stmt)]


class RunView(BaseModel):
    """Everything stored about one run, assembled for the result page."""

    run: RunRow
    overall_error: str | None = None
    events: list[StageEvent] = Field(default_factory=list)
    invoice: NormalizedInvoice | None = None
    decision: DecisionResult | None = None
    ai_record: AiExtractionRecord | None = None
    ingestion: dict | None = None  # how the document was read (page methods, OCR confidence), text-free
    error: PipelineError | None = None  # set when the run failed


def get_run_view(session: Session, run_id: str) -> RunView | None:
    run = repo.get_run(session, run_id)
    if run is None:
        return None
    inv = session.scalars(select(Invoice).where(Invoice.processing_run_id == run.id)).first()
    saved = (inv.extraction_json or {}) if inv else {}

    events = [
        StageEvent(
            stage_name=e.stage_name,
            title=_TITLES.get(e.stage_name, e.stage_name),
            status=e.stage_status,  # type: ignore[arg-type]
            message=e.message,
            started_at=e.started_at,
            completed_at=e.completed_at,
            duration_ms=e.duration_ms,
        )
        for e in repo.list_events(session, run_id)
    ]
    invoice = None
    if saved.get("invoice"):
        invoice = NormalizedInvoice.model_validate(saved["invoice"])
    elif inv and inv.invoice_number:
        invoice = NormalizedInvoice(
            invoice_number=inv.invoice_number,
            vendor_name=inv.vendor_name,
            vendor_tax_id=inv.vendor_tax_id,
            invoice_date=inv.invoice_date,
            purchase_order_number=inv.po_number,
            currency=inv.currency,
            subtotal=inv.subtotal,
            tax_total=inv.tax_total,
            grand_total=inv.grand_total,
        )
    decision = DecisionResult.model_validate(inv.decision_explanation) if inv and inv.decision_explanation else None
    record = repo.get_extraction_record(session, saved["extraction_record_id"]) if saved.get("extraction_record_id") else None

    error = None
    if run.run_status == constants.RUN_FAILED:
        code = saved.get("failure_code", "internal_error")
        error = PipelineError(
            stage=saved.get("failed_stage", run.current_stage or "unknown"),
            code=code,
            message=(decision.reasons[0].message if decision and decision.reasons else run.overall_error) or "The run failed.",
            action=ACTIONS.get(code, ACTIONS["internal_error"]),
            manual_entry_possible=code
            in {
                "config_error",
                "auth_error",
                "rate_limited",
                "service_unavailable",
                "invalid_response",
                "blocked",
                "bad_request",
                "empty_extraction",
            },
        )
    return RunView(
        run=_row(run, inv),
        overall_error=run.overall_error,
        events=events,
        invoice=invoice,
        decision=decision,
        ai_record=record,
        ingestion=saved.get("ingestion"),
        error=error,
    )


# ============================================================================= purchase orders


class PoRow(BaseModel):
    po_number: str
    vendor_name: str
    currency: str
    status: str
    po_date: date
    total_amount: Decimal
    tolerance_percent: Decimal
    approved_amount: Decimal  # approved invoices against this PO, on the configured comparison basis
    remaining: Decimal  # total - approved
    allowed_overage: Decimal
    maximum_allowed: Decimal  # remaining + allowed overage
    approved_invoice_count: int
    problems: list[str] = Field(default_factory=list)  # approved invoices whose data prevents a reliable balance


def _approved_by_po(session: Session) -> dict[str, list[Invoice]]:
    grouped: dict[str, list[Invoice]] = {}
    for inv in session.scalars(select(Invoice).where(Invoice.final_status.in_(constants.APPROVED_STATUSES)).order_by(Invoice.created_at)):
        key = normalize_po_number(inv.po_number)
        if key:
            grouped.setdefault(key, []).append(inv)
    return grouped


def _po_row(po: PurchaseOrder, approved: list[Invoice], config: DecisionConfig) -> PoRow:
    refs: list[PriorInvoiceRef] = [prior_ref(i) for i in approved]
    bal = compute_po_balance(po_snapshot(po), refs, config.po_amount_basis)
    return PoRow(
        po_number=po.po_number,
        vendor_name=po.vendor_name,
        currency=po.currency,
        status=po.status,
        po_date=po.po_date,
        total_amount=po.total_amount,
        tolerance_percent=po.tolerance_percent,
        approved_amount=bal.approved_total,
        remaining=bal.remaining,
        allowed_overage=bal.allowed_overage,
        maximum_allowed=bal.maximum_allowed,
        approved_invoice_count=len(approved),
        problems=bal.problems,
    )


def po_overview(
    session: Session, config: DecisionConfig, *, search: str | None = None, status: str | None = None, currency: str | None = None
) -> list[PoRow]:
    """All purchase orders with their approved invoiced amount and remaining balance (same maths as the engine)."""
    approved = _approved_by_po(session)
    rows = []
    needle = (search or "").strip().lower()
    for po in repo.list_purchase_orders(session, status=status, currency=currency):
        if (
            needle
            and needle not in po.po_number.lower()
            and needle not in po.vendor_name.lower()
            and needle not in (normalize_vendor_name(po.vendor_name) or "")
        ):
            continue
        rows.append(_po_row(po, approved.get(normalize_po_number(po.po_number) or "", []), config))
    return rows


def po_approved_invoices(session: Session, po_number: str) -> list[RunRow]:
    """The approved invoices counted against one PO, as history rows."""
    out = []
    for inv in repo.get_approved_invoices_for_po(session, po_number):
        out.append(_row(inv.run, inv))
    return out


# ============================================================================= CSV report


CSV_COLUMNS = [
    "run_id",
    "processed_at_utc",
    "file_name",
    "file_sha256",
    "run_status",
    "decision",
    "decision_reasons",
    "invoice_number",
    "vendor",
    "po_number",
    "currency",
    "subtotal",
    "tax_total",
    "grand_total",
    "duration_seconds",
]


def _safe(value: object) -> str:
    """Neutralise spreadsheet formula injection: vendor names and file names come from untrusted documents."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA_PREFIXES) else text


def runs_to_csv(rows: Sequence[RunRow]) -> str:
    """Invoice-processing report. Amounts are plain decimal strings (no symbols, no float rounding)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.run_id,
                r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                _safe(r.filename),
                r.file_hash,
                r.run_status,
                r.decision_status or "",
                _safe("; ".join(r.reasons)),
                _safe(r.invoice_number),
                _safe(r.vendor_name),
                _safe(r.po_number),
                r.currency or "",
                "" if r.subtotal is None else f"{r.subtotal:f}",
                "" if r.tax_total is None else f"{r.tax_total:f}",
                "" if r.grand_total is None else f"{r.grand_total:f}",
                "" if r.duration_seconds is None else f"{r.duration_seconds:.2f}",
            ]
        )
    return out.getvalue()
