"""SQLAlchemy ORM models (SQLite locally, PostgreSQL when deployed).

Design notes
* Uploaded PDFs are never stored: only hashes, extracted fields, decisions and processing events.
* Money columns use ``DecimalType`` (exact on SQLite too); timestamps use ``UTCDateTime``.
* ``invoices.po_number`` is deliberately *not* a foreign key: an invoice may cite a PO that does not
  exist, and recording exactly that is part of the audit trail.
* Schema changes go through ``database/migrations.py``; never edit a released table in place.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from invoiceflow import constants
from invoiceflow.database.types import DecimalType, UTCDateTime, utcnow


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class SchemaInfo(Base):
    """Key/value bookkeeping; ``schema_version`` is managed by ``migrations.py``."""

    __tablename__ = "schema_info"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


class ExtractionRecordRow(Base):
    """Audit trail of one extraction attempt (AI or manual). See ``schemas.AiExtractionRecord``."""

    __tablename__ = "extraction_records"

    record_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    file_sha256: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extraction_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # validated strings
    normalized_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # Decimals as strings
    evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint(_in("status", constants.PO_STATUSES), name="ck_po_status"),
        CheckConstraint("total_amount >= 0", name="ck_po_total_nonneg"),
        CheckConstraint("length(currency) = 3", name="ck_po_currency_len"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_vendor_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(DecimalType(18, 2), nullable=False)
    tolerance_percent: Mapped[Decimal] = mapped_column(DecimalType(5, 2), nullable=False, default=Decimal("0.00"))
    po_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default=constants.PO_OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (CheckConstraint(_in("run_status", constants.RUN_STATUSES), name="ck_run_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # SHA-256 of the PDF
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_status: Mapped[str] = mapped_column(String(16), nullable=False, default=constants.RUN_RUNNING, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    overall_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list[ProcessingEvent]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="ProcessingEvent.id")
    invoice: Mapped[Invoice | None] = relationship(back_populates="run", cascade="all, delete-orphan", uselist=False)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("processing_run_id", name="uq_invoice_one_per_run"),
        CheckConstraint(_in("final_status", constants.INVOICE_STATUSES), name="ck_invoice_status"),
        Index("ix_invoices_vendor_number", "normalized_vendor_name", "invoice_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    po_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # not an FK, see module doc
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(DecimalType(18, 2), nullable=True)
    tax_total: Mapped[Decimal | None] = mapped_column(DecimalType(18, 2), nullable=True)
    grand_total: Mapped[Decimal | None] = mapped_column(DecimalType(18, 2), nullable=True)
    extraction_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    decision_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    # Full explanation of the decision (checks, amounts, human actions); ``decision_reasons`` keeps the codes.
    decision_explanation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    run: Mapped[ProcessingRun] = relationship(back_populates="invoice")


class ProcessingEvent(Base):
    __tablename__ = "processing_events"
    __table_args__ = (
        UniqueConstraint("processing_run_id", "stage_name", name="uq_event_run_stage"),
        CheckConstraint(_in("stage_status", constants.STAGE_STATUSES), name="ck_event_status"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_event_duration"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False)
    stage_status: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped[ProcessingRun] = relationship(back_populates="events")
