"""Data-access functions.

Conventions
* Every function takes a ``Session`` and **never commits**: the caller owns the transaction
  (``with session_scope() as s:``). Pipeline stages therefore use one short transaction each, so a
  live view can see progress while a run is still going.
* Money is ``Decimal`` end to end. Sums are computed in Python, not SQL (see ``DecimalType``).
* Uploaded files are never stored here: only hashes, extracted fields, decisions and events.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import PurePath
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from invoiceflow import constants
from invoiceflow.database.migrations import DECISION_LOCK_KEY
from invoiceflow.database.models import (
    ExtractionRecordRow,
    Invoice,
    ProcessingEvent,
    ProcessingRun,
    PurchaseOrder,
    SchemaInfo,
)
from invoiceflow.database.types import utcnow
from invoiceflow.schemas import AiExtractionRecord, DashboardStats, NormalizedInvoice, PurchaseOrderData
from invoiceflow.services.normalizer import (
    compute_content_hash,
    normalize_invoice_number,
    normalize_po_number,
    normalize_vendor_name,
)


class RepositoryError(ValueError):
    """Invalid input or state for a repository operation."""


# ============================================================================= extraction audit records


def save_extraction_record(session: Session, record: AiExtractionRecord) -> None:
    """Insert or replace the audit row for ``record`` (Decimals/dates are stored as JSON strings)."""
    dump = record.model_dump(mode="json")
    session.merge(
        ExtractionRecordRow(
            record_id=record.record_id,
            file_sha256=record.file_sha256,
            source=record.source,
            status=record.status,
            model=record.model,
            extracted_at=record.extracted_at,
            attempts=record.attempts,
            extraction_json=dump["extraction"],
            normalized_json=dump["normalized"],
            evidence_json=dump["evidence"],
            warnings_json=dump["warnings"],
            raw_response=record.raw_response,
            error_code=record.error_code,
            error_message=record.error_message,
        )
    )


def _to_record(row: ExtractionRecordRow) -> AiExtractionRecord:
    return AiExtractionRecord.model_validate(
        {
            "record_id": row.record_id,
            "file_sha256": row.file_sha256,
            "source": row.source,
            "status": row.status,
            "model": row.model,
            "extracted_at": row.extracted_at,
            "attempts": row.attempts,
            "extraction": row.extraction_json,
            "normalized": row.normalized_json,
            "evidence": row.evidence_json or [],
            "warnings": row.warnings_json or [],
            "raw_response": row.raw_response,
            "error_code": row.error_code,
            "error_message": row.error_message,
        }
    )


def get_extraction_record(session: Session, record_id: str) -> AiExtractionRecord | None:
    row = session.get(ExtractionRecordRow, record_id)
    return _to_record(row) if row else None


def list_extraction_records_for_file(session: Session, file_sha256: str) -> list[AiExtractionRecord]:
    rows = session.scalars(
        select(ExtractionRecordRow).where(ExtractionRecordRow.file_sha256 == file_sha256).order_by(ExtractionRecordRow.extracted_at)
    )
    return [_to_record(r) for r in rows]


# ============================================================================= purchase orders


def create_purchase_order_if_missing(session: Session, data: PurchaseOrderData) -> tuple[PurchaseOrder, bool]:
    """Insert the PO unless its ``po_number`` already exists. Existing rows are never modified.

    Returns ``(row, created)``.
    """
    existing = get_purchase_order(session, data.po_number)
    if existing is not None:
        return existing, False
    row = PurchaseOrder(
        po_number=data.po_number,
        vendor_name=data.vendor_name,
        normalized_vendor_name=normalize_vendor_name(data.vendor_name) or data.vendor_name.casefold(),
        currency=data.currency,
        total_amount=data.total_amount,
        tolerance_percent=data.tolerance_percent,
        po_date=data.po_date,
        status=data.status,
    )
    session.add(row)
    session.flush()
    return row, True


def _po_key(column):
    """SQL expression matching ``normalize_po_number``: lower-case with all spaces removed."""
    return func.replace(func.lower(column), " ", "")


def find_purchase_orders_by_number(session: Session, po_number: str | None) -> list[PurchaseOrder]:
    """Every PO whose number equals ``po_number`` after normalisation (case and spaces ignored).

    Normally zero or one row. More than one means the number is ambiguous and must not be guessed.
    """
    key = normalize_po_number(po_number)
    if key is None:
        return []
    return list(session.scalars(select(PurchaseOrder).where(_po_key(PurchaseOrder.po_number) == key).order_by(PurchaseOrder.id)))


def get_purchase_order(session: Session, po_number: str) -> PurchaseOrder | None:
    """First PO matching the normalised number (case-insensitive, spaces ignored), or ``None``."""
    rows = find_purchase_orders_by_number(session, po_number)
    return rows[0] if rows else None


def list_candidate_purchase_orders(session: Session, vendor_name: str | None) -> list[PurchaseOrder]:
    """POs whose loose vendor key equals the vendor's. A *shortlist*: confirm with ``compare_vendors``."""
    key = normalize_vendor_name(vendor_name)
    if key is None:
        return []
    return list(session.scalars(select(PurchaseOrder).where(PurchaseOrder.normalized_vendor_name == key).order_by(PurchaseOrder.po_number)))


def list_purchase_orders(
    session: Session, *, status: str | None = None, vendor: str | None = None, currency: str | None = None
) -> list[PurchaseOrder]:
    stmt = select(PurchaseOrder).order_by(PurchaseOrder.po_number)
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    if currency:
        stmt = stmt.where(PurchaseOrder.currency == currency.upper())
    if vendor:
        stmt = stmt.where(PurchaseOrder.normalized_vendor_name == (normalize_vendor_name(vendor) or vendor.casefold()))
    return list(session.scalars(stmt))


def count_purchase_orders(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(PurchaseOrder)) or 0


# ============================================================================= processing runs & events


def _safe_filename(name: str | None) -> str:
    """Basename only (no path), control characters removed, at most 255 characters."""
    base = PurePath((name or "").replace("\\", "/")).name
    cleaned = "".join(ch for ch in base if ch.isprintable()).strip()
    return (cleaned or "unnamed.pdf")[:255]


def create_processing_run(session: Session, original_filename: str | None, file_hash: str) -> ProcessingRun:
    if len(file_hash) != 64 or any(c not in "0123456789abcdefABCDEF" for c in file_hash):
        raise RepositoryError("file_hash must be a 64-character SHA-256 hex digest")
    run = ProcessingRun(
        run_id=uuid.uuid4().hex,
        original_filename=_safe_filename(original_filename),
        file_hash=file_hash.lower(),
        run_status=constants.RUN_RUNNING,
        started_at=utcnow(),
    )
    session.add(run)
    session.flush()
    return run


def get_run(session: Session, run_id: str) -> ProcessingRun | None:
    return session.scalars(select(ProcessingRun).where(ProcessingRun.run_id == run_id)).first()


def _require_run(session: Session, run_id: str) -> ProcessingRun:
    run = get_run(session, run_id)
    if run is None:
        raise RepositoryError(f"Unknown processing run {run_id!r}")
    return run


def update_stage(
    session: Session,
    run_id: str,
    stage_name: str,
    stage_status: str,
    message: str | None = None,
    *,
    at: datetime | None = None,
    duration_ms: int | None = None,
) -> ProcessingEvent:
    """Create or update the event row for one stage of a run (one row per run and stage).

    ``PENDING`` only records the plan (no timestamps). ``RUNNING`` stamps ``started_at`` and moves the
    run's ``current_stage``; ``COMPLETED`` / ``FAILED`` / ``SKIPPED`` stamp ``completed_at`` and
    ``duration_ms``. A failed stage does not itself fail the run: call :func:`complete_run`.
    ``at`` / ``duration_ms`` let a caller that measured the stage itself store exactly its own values.
    """
    if stage_status not in constants.STAGE_STATUSES:
        raise RepositoryError(f"Invalid stage status {stage_status!r}")
    run = _require_run(session, run_id)
    now = at or utcnow()
    event = session.scalars(
        select(ProcessingEvent).where(ProcessingEvent.processing_run_id == run.id, ProcessingEvent.stage_name == stage_name)
    ).first()
    if event is None:
        event = ProcessingEvent(processing_run_id=run.id, stage_name=stage_name, stage_status=stage_status)
        session.add(event)
    event.stage_status = stage_status
    if message is not None:
        event.message = message
    if stage_status == constants.STAGE_PENDING:
        event.started_at = event.completed_at = event.duration_ms = None
    elif stage_status == constants.STAGE_RUNNING:
        event.started_at, event.completed_at, event.duration_ms = now, None, None
        run.current_stage = stage_name
    elif stage_status == constants.STAGE_SKIPPED and event.started_at is None:
        event.completed_at, event.duration_ms = now, 0  # never started: skipped, so it took no time
    else:
        event.started_at = event.started_at or now
        event.completed_at = now
        event.duration_ms = duration_ms if duration_ms is not None else max(0, int((now - event.started_at).total_seconds() * 1000))
        if stage_status != constants.STAGE_SKIPPED:
            run.current_stage = stage_name  # a failed run keeps pointing at the stage where it stopped
    session.flush()
    return event


def list_events(session: Session, run_id: str) -> list[ProcessingEvent]:
    run = _require_run(session, run_id)
    return list(session.scalars(select(ProcessingEvent).where(ProcessingEvent.processing_run_id == run.id).order_by(ProcessingEvent.id)))


def complete_run(session: Session, run_id: str, status: str, error: str | None = None) -> ProcessingRun:
    """Finish a run as ``COMPLETED`` or ``FAILED`` (``error`` is required for a failure)."""
    if status not in (constants.RUN_COMPLETED, constants.RUN_FAILED):
        raise RepositoryError(f"A run can only be completed as COMPLETED or FAILED, not {status!r}")
    if status == constants.RUN_FAILED and not error:
        raise RepositoryError("A failed run must record why (overall_error)")
    run = _require_run(session, run_id)
    run.run_status = status
    run.completed_at = utcnow()
    run.overall_error = error if status == constants.RUN_FAILED else None
    session.flush()
    return run


# ============================================================================= invoices


def save_invoice(
    session: Session,
    run_id: str,
    normalized: NormalizedInvoice | None,
    *,
    final_status: str,
    decision_reasons: list[str] | None = None,
    extraction_json: dict[str, Any] | None = None,
    decision_explanation: dict[str, Any] | None = None,
) -> Invoice:
    """Store (or replace) the invoice result of a run.

    ``normalized`` may be ``None`` for a failed extraction, in which case only the status, reasons and
    ``extraction_json`` are recorded. Re-saving for the same run updates the existing row.
    ``decision_explanation`` is the JSON form of the engine's ``DecisionResult``.
    """
    if final_status not in constants.INVOICE_STATUSES:
        raise RepositoryError(f"Invalid invoice status {final_status!r}")
    run = _require_run(session, run_id)
    n = normalized or NormalizedInvoice()
    values: dict[str, Any] = dict(
        invoice_number=n.invoice_number,
        vendor_name=n.vendor_name,
        normalized_vendor_name=normalize_vendor_name(n.vendor_name),
        vendor_tax_id=n.vendor_tax_id,
        invoice_date=n.invoice_date,
        po_number=n.purchase_order_number,
        currency=n.currency,
        subtotal=n.subtotal,
        tax_total=n.tax_total,
        grand_total=n.grand_total,
        extraction_json=extraction_json if extraction_json is not None else (n.model_dump(mode="json") if normalized else None),
        final_status=final_status,
        decision_reasons=list(decision_reasons or []),
        decision_explanation=decision_explanation,
        content_hash=compute_content_hash(n.vendor_name, n.invoice_number, n.grand_total, n.invoice_date)
        if normalized and n.invoice_number
        else None,
    )
    invoice = session.scalars(select(Invoice).where(Invoice.processing_run_id == run.id)).first()
    if invoice is None:
        invoice = Invoice(processing_run_id=run.id, **values)
        session.add(invoice)
    else:
        for key, value in values.items():
            setattr(invoice, key, value)
    session.flush()
    return invoice


def update_invoice_decision(session: Session, run_id: str, final_status: str, decision_reasons: list[str] | None = None) -> Invoice:
    """Change an invoice's status (e.g. a human approves it). Reasons are replaced when given.

    SECURITY: this is the only function that can set an approval outside the decision engine, and NOTHING in
    the application calls it. Do not expose it through the UI until there is real user authentication and
    an audit trail of who decided; the public demo deliberately offers no approve/reject action.
    """
    if final_status not in constants.INVOICE_STATUSES:
        raise RepositoryError(f"Invalid invoice status {final_status!r}")
    run = _require_run(session, run_id)
    invoice = session.scalars(select(Invoice).where(Invoice.processing_run_id == run.id)).first()
    if invoice is None:
        raise RepositoryError(f"Run {run_id!r} has no saved invoice")
    invoice.final_status = final_status
    if decision_reasons is not None:
        invoice.decision_reasons = list(decision_reasons)
    session.flush()
    return invoice


def get_approved_invoices_for_po(session: Session, po_number: str, *, exclude_run_id: str | None = None) -> list[Invoice]:
    """Invoices against ``po_number`` that count towards its balance: status APPROVED only.

    Review-pending, duplicate-blocked, failed and rejected invoices never count. ``exclude_run_id``
    leaves out the invoice currently being (re)decided.
    """
    key = normalize_po_number(po_number)
    if key is None:
        return []
    stmt = select(Invoice).where(_po_key(Invoice.po_number) == key, Invoice.final_status.in_(constants.APPROVED_STATUSES))
    if exclude_run_id:
        excluded = get_run(session, exclude_run_id)
        if excluded is not None:
            stmt = stmt.where(Invoice.processing_run_id != excluded.id)
    return list(session.scalars(stmt.order_by(Invoice.created_at, Invoice.id)))


def search_invoices(
    session: Session,
    *,
    invoice_number: str | None = None,
    vendor: str | None = None,
    po_number: str | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Invoice]:
    """Search stored invoices, newest first. Text filters are case-insensitive substring matches."""
    if limit < 1 or offset < 0:
        raise RepositoryError("limit must be >= 1 and offset >= 0")
    stmt = select(Invoice).order_by(Invoice.created_at.desc(), Invoice.id.desc())
    if invoice_number:
        stmt = stmt.where(func.lower(Invoice.invoice_number).like(f"%{_like(invoice_number)}%", escape="\\"))
    if vendor:
        key = normalize_vendor_name(vendor) or vendor.casefold()
        stmt = stmt.where(
            or_(
                Invoice.normalized_vendor_name.like(f"%{_like(key)}%", escape="\\"),
                func.lower(Invoice.vendor_name).like(f"%{_like(vendor.casefold())}%", escape="\\"),
            )
        )
    if po_number:
        stmt = stmt.where(func.lower(Invoice.po_number).like(f"%{_like(po_number)}%", escape="\\"))
    if status:
        stmt = stmt.where(Invoice.final_status == status)
    if date_from:
        stmt = stmt.where(Invoice.invoice_date >= date_from)
    if date_to:
        stmt = stmt.where(Invoice.invoice_date <= date_to)
    return list(session.scalars(stmt.limit(limit).offset(offset)))


def _like(text: str) -> str:
    """Escape LIKE wildcards in user input and lower-case it."""
    return text.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_duplicate_invoices(
    session: Session,
    *,
    vendor_name: str | None,
    invoice_number: str | None,
    vendor_tax_id: str | None = None,
    exclude_run_id: str | None = None,
) -> list[Invoice]:
    """Stored invoices with the same invoice number from the same vendor.

    "Same vendor" means the same normalised name (``Acme Ltd`` == ``ACME Limited``) or the same tax
    id. Invoice numbers compare case-insensitively ignoring whitespace. Pass ``exclude_run_id`` so the
    invoice currently being processed does not match itself. Returns oldest first.
    """
    wanted = normalize_invoice_number(invoice_number)
    if not wanted:
        return []
    vendor_key = normalize_vendor_name(vendor_name)
    tax_id = (vendor_tax_id or "").strip()
    vendor_clauses = []
    if vendor_key:
        vendor_clauses.append(Invoice.normalized_vendor_name == vendor_key)
    if tax_id:
        vendor_clauses.append(func.lower(Invoice.vendor_tax_id) == tax_id.lower())
    if not vendor_clauses:
        return []
    stmt = select(Invoice).where(or_(*vendor_clauses), Invoice.invoice_number.is_not(None))
    if exclude_run_id:
        excluded = get_run(session, exclude_run_id)
        if excluded is not None:
            stmt = stmt.where(Invoice.processing_run_id != excluded.id)
    rows = session.scalars(stmt.order_by(Invoice.created_at, Invoice.id))
    return [inv for inv in rows if normalize_invoice_number(inv.invoice_number) == wanted]


def find_invoices_by_content_hash(session: Session, content_hash: str, *, exclude_run_id: str | None = None) -> list[Invoice]:
    """Invoices with identical vendor + number + total + date fingerprint (catches re-keyed duplicates)."""
    stmt = select(Invoice).where(Invoice.content_hash == content_hash)
    if exclude_run_id:
        excluded = get_run(session, exclude_run_id)
        if excluded is not None:
            stmt = stmt.where(Invoice.processing_run_id != excluded.id)
    return list(session.scalars(stmt.order_by(Invoice.created_at, Invoice.id)))


def find_prior_invoices_by_file_hash(session: Session, file_hash: str, *, exclude_run_id: str | None = None) -> list[Invoice]:
    """Stored invoices whose source file had exactly this SHA-256 (any status; the engine ignores failed attempts)."""
    stmt = (
        select(Invoice)
        .join(ProcessingRun, Invoice.processing_run_id == ProcessingRun.id)
        .where(ProcessingRun.file_hash == file_hash.lower())
    )
    if exclude_run_id:
        stmt = stmt.where(ProcessingRun.run_id != exclude_run_id)
    return list(session.scalars(stmt.order_by(Invoice.created_at, Invoice.id)))


def find_runs_by_file_hash(session: Session, file_hash: str, *, exclude_run_id: str | None = None) -> list[ProcessingRun]:
    """Earlier runs that processed a byte-identical file."""
    stmt = select(ProcessingRun).where(ProcessingRun.file_hash == file_hash.lower())
    if exclude_run_id:
        stmt = stmt.where(ProcessingRun.run_id != exclude_run_id)
    return list(session.scalars(stmt.order_by(ProcessingRun.started_at, ProcessingRun.id)))


# ============================================================================= decision consistency


def acquire_decision_lock(session: Session) -> None:
    """Serialise decisions: take a write lock held until this transaction commits or rolls back.

    Two invoices decided at the same moment must not both be checked against the same stale PO balance
    (or both pass the duplicate check). Call this FIRST in a fresh transaction, then read the totals,
    decide and save inside that same transaction. PostgreSQL blocks on the row lock; SQLite blocks on
    its database write lock (so keep the transaction short).
    """
    stmt = update(SchemaInfo).where(SchemaInfo.key == DECISION_LOCK_KEY).values(value=SchemaInfo.value)
    if session.execute(stmt).rowcount == 0:  # row missing (hand-made database): create it, then lock it
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        session.execute(insert(SchemaInfo).values(key=DECISION_LOCK_KEY, value="1").on_conflict_do_nothing())
        session.execute(stmt)


# ============================================================================= dashboard


def get_dashboard_stats(session: Session, *, now: datetime | None = None) -> DashboardStats:
    now = now or datetime.now(UTC)
    stats = DashboardStats()

    runs = session.execute(select(ProcessingRun.run_status, ProcessingRun.started_at, ProcessingRun.completed_at)).all()
    stats.total_runs = len(runs)
    for status, _, _ in runs:
        stats.runs_by_status[status] = stats.runs_by_status.get(status, 0) + 1
    durations = [(done - start).total_seconds() for _, start, done in runs if done is not None]
    stats.avg_run_duration_seconds = round(sum(durations) / len(durations), 3) if durations else None
    stats.runs_last_7_days = sum(1 for _, start, _ in runs if start >= now - timedelta(days=7))

    not_distinct = (constants.DECISION_DUPLICATE_BLOCKED, constants.DECISION_PROCESSING_FAILED)
    for status, currency, total in session.execute(select(Invoice.final_status, Invoice.currency, Invoice.grand_total)):
        stats.total_invoices += 1
        stats.invoices_by_status[status] = stats.invoices_by_status.get(status, 0) + 1
        # The processed amount counts each distinct invoice once: blocked duplicates and failed runs are excluded.
        if currency and total is not None and status not in not_distinct:
            stats.invoiced_total_by_currency[currency] = stats.invoiced_total_by_currency.get(currency, Decimal(0)) + total
            if status in constants.APPROVED_STATUSES:
                stats.approved_total_by_currency[currency] = stats.approved_total_by_currency.get(currency, Decimal(0)) + total

    for status, count in session.execute(select(PurchaseOrder.status, func.count()).group_by(PurchaseOrder.status)):
        if status == constants.PO_OPEN:
            stats.open_purchase_orders = count
        elif status == constants.PO_CLOSED:
            stats.closed_purchase_orders = count
    return stats
