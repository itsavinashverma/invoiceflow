"""Data-access functions. More repositories (POs, invoices, runs) arrive with the data-model stage."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.database.models import ExtractionRecordRow
from invoiceflow.schemas import AiExtractionRecord


def save_extraction_record(session: Session, record: AiExtractionRecord) -> None:
    """Insert or replace the audit row for ``record`` (Decimals/dates are stored as JSON strings)."""
    dump = record.model_dump(mode="json")
    session.merge(ExtractionRecordRow(
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
    ))


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every timestamp we store is UTC, so restore the zone."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _to_record(row: ExtractionRecordRow) -> AiExtractionRecord:
    return AiExtractionRecord.model_validate({
        "record_id": row.record_id,
        "file_sha256": row.file_sha256,
        "source": row.source,
        "status": row.status,
        "model": row.model,
        "extracted_at": _utc(row.extracted_at),
        "attempts": row.attempts,
        "extraction": row.extraction_json,
        "normalized": row.normalized_json,
        "evidence": row.evidence_json or [],
        "warnings": row.warnings_json or [],
        "raw_response": row.raw_response,
        "error_code": row.error_code,
        "error_message": row.error_message,
    })


def get_extraction_record(session: Session, record_id: str) -> AiExtractionRecord | None:
    row = session.get(ExtractionRecordRow, record_id)
    return _to_record(row) if row else None


def list_extraction_records_for_file(session: Session, file_sha256: str) -> list[AiExtractionRecord]:
    rows = session.scalars(
        select(ExtractionRecordRow)
        .where(ExtractionRecordRow.file_sha256 == file_sha256)
        .order_by(ExtractionRecordRow.extracted_at)
    )
    return [_to_record(r) for r in rows]
