"""SQLAlchemy ORM models.

Only the declarative ``Base`` and a tiny ``SchemaInfo`` table (proves the database can be
initialised and records the schema version) exist so far. Business tables (vendors, purchase
orders, processed invoices, runs, ...) arrive with the data-model stage.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = "0"


class Base(DeclarativeBase):
    pass


class SchemaInfo(Base):
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
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extraction_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # validated strings
    normalized_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # Decimals as strings
    evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
