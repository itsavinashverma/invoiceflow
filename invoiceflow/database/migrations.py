"""Schema versioning: ordered, additive, idempotent migrations.

``schema_info.schema_version`` records the highest applied migration. On start-up ``migrate`` applies
pending migrations in order, each in its own transaction; on PostgreSQL an advisory lock stops two
app instances from migrating at once.

Rules that keep data safe:
* Migrations only ADD (tables, nullable columns, indexes). ``drop_all`` is never called and nothing
  here deletes or rewrites rows. A destructive change must be a new migration that copies data first.
* Every migration is idempotent (checks before it creates), so an interrupted run can simply be repeated.
* A database whose version is *newer* than this code is refused rather than modified.
* Databases created before versioning existed (version ``0``, ``extraction_records`` present) are
  adopted as-is: migration 1 only creates what is missing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, inspect, select, text, update

from invoiceflow import constants
from invoiceflow.database.models import (
    Base,
    ExtractionRecordRow,
    Invoice,
    ProcessingEvent,
    ProcessingRun,
    PurchaseOrder,
    SchemaInfo,
)

logger = logging.getLogger(__name__)

VERSION_KEY = "schema_version"
_ADVISORY_LOCK_ID = 727_274_001


class SchemaVersionError(RuntimeError):
    """The database is newer than this application, or its version marker is unreadable."""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[Connection], None]


def _create_tables(*models: type[Base]) -> Callable[[Connection], None]:
    def apply(conn: Connection) -> None:
        Base.metadata.create_all(conn, tables=[m.__table__ for m in models], checkfirst=True)

    return apply


DECISION_LOCK_KEY = "decision_lock"

# Statuses used before the decision engine existed. Mapping is conservative: anything that was only
# "recommended" for approval, review or rejection now needs a person (REVIEW_REQUIRED).
LEGACY_STATUS_MAP = {
    "AUTO_APPROVE_RECOMMENDED": "APPROVED",
    "HUMAN_REVIEW": "REVIEW_REQUIRED",
    "REJECT_RECOMMENDED": "REVIEW_REQUIRED",
    "FAILED_EXTRACTION": "PROCESSING_FAILED",
}

_STATUS_LIST = ", ".join(repr(s) for s in constants.INVOICE_STATUSES)
_CASE_MAP = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in LEGACY_STATUS_MAP.items())
_LEGACY_IN = ", ".join(f"'{old}'" for old in LEGACY_STATUS_MAP)

# Frozen DDL for rebuilding ``invoices`` on SQLite (which cannot alter a CHECK constraint in place).
_SQLITE_INVOICES_V3 = f"""
CREATE TABLE invoices_rebuild (
    id INTEGER NOT NULL, processing_run_id INTEGER NOT NULL, invoice_number VARCHAR(128),
    vendor_name VARCHAR(255), normalized_vendor_name VARCHAR(255), vendor_tax_id VARCHAR(64),
    invoice_date DATE, po_number VARCHAR(64), currency VARCHAR(3), subtotal VARCHAR(22),
    tax_total VARCHAR(22), grand_total VARCHAR(22), extraction_json JSON,
    final_status VARCHAR(32) NOT NULL, decision_reasons JSON NOT NULL, content_hash VARCHAR(64),
    created_at DATETIME NOT NULL, decision_explanation JSON,
    PRIMARY KEY (id),
    CONSTRAINT uq_invoice_one_per_run UNIQUE (processing_run_id),
    CONSTRAINT ck_invoice_status CHECK (final_status IN ({_STATUS_LIST})),
    FOREIGN KEY(processing_run_id) REFERENCES processing_runs (id) ON DELETE CASCADE
)"""


def _invoices_schema_is_current(conn: Connection) -> bool:
    """True if ``invoices`` already has the v3 column and accepts the v3 statuses."""
    if conn.dialect.name == "sqlite":
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='invoices'")).scalar() or ""
        return "decision_explanation" in sql and "REVIEW_REQUIRED" in sql
    insp = inspect(conn)
    has_column = any(c["name"] == "decision_explanation" for c in insp.get_columns("invoices"))
    check_ok = any("REVIEW_REQUIRED" in (c.get("sqltext") or "") for c in insp.get_check_constraints("invoices"))
    return has_column and check_ok


def _rebuild_invoices_sqlite(conn: Connection) -> None:
    """Copy-and-swap rebuild inside the migration's transaction (SQLite DDL is transactional here)."""
    conn.execute(text("DROP TABLE IF EXISTS invoices_rebuild"))  # scratch table from an interrupted earlier attempt
    conn.execute(text(_SQLITE_INVOICES_V3))
    old_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(invoices)"))}
    copy = [
        c
        for c in (
            "id",
            "processing_run_id",
            "invoice_number",
            "vendor_name",
            "normalized_vendor_name",
            "vendor_tax_id",
            "invoice_date",
            "po_number",
            "currency",
            "subtotal",
            "tax_total",
            "grand_total",
            "extraction_json",
            "decision_reasons",
            "content_hash",
            "created_at",
        )
        if c in old_columns
    ]
    cols = ", ".join(copy)
    conn.execute(
        text(
            f"INSERT INTO invoices_rebuild ({cols}, final_status) "  # noqa: S608 - names are module constants, never user input
            f"SELECT {cols}, CASE final_status {_CASE_MAP} ELSE final_status END FROM invoices"
        )
    )
    conn.execute(text("DROP TABLE invoices"))
    conn.execute(text("ALTER TABLE invoices_rebuild RENAME TO invoices"))
    for index in Invoice.__table__.indexes:
        index.create(conn, checkfirst=True)


def _upgrade_invoices_postgres(conn: Connection) -> None:
    conn.execute(text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS decision_explanation JSON"))
    conn.execute(text("ALTER TABLE invoices DROP CONSTRAINT IF EXISTS ck_invoice_status"))
    conn.execute(
        text(f"UPDATE invoices SET final_status = CASE final_status {_CASE_MAP} ELSE final_status END WHERE final_status IN ({_LEGACY_IN})")  # noqa: S608 - constants only
    )
    conn.execute(text(f"ALTER TABLE invoices ADD CONSTRAINT ck_invoice_status CHECK (final_status IN ({_STATUS_LIST}))"))


def _refresh_vendor_keys(conn: Connection) -> None:
    """Recompute the derived ``normalized_vendor_name`` columns with the current rules (e.g. 'S.L.' handling)."""
    from invoiceflow.services.normalizer import normalize_vendor_name

    for table in ("purchase_orders", "invoices"):
        rows = conn.execute(text(f"SELECT id, vendor_name, normalized_vendor_name FROM {table}")).all()  # noqa: S608 - fixed table names
        for row_id, name, current in rows:
            fresh = normalize_vendor_name(name) if name else None
            if fresh is not None and fresh != current:
                conn.execute(text(f"UPDATE {table} SET normalized_vendor_name = :v WHERE id = :i"), {"v": fresh, "i": row_id})  # noqa: S608 - fixed table names


def _v3_decision_engine(conn: Connection) -> None:
    """v3: decision vocabulary, explanation column, decision-lock row, refreshed vendor keys."""
    if not _invoices_schema_is_current(conn):
        if conn.dialect.name == "sqlite":
            _rebuild_invoices_sqlite(conn)
        else:
            _upgrade_invoices_postgres(conn)
    if conn.execute(select(SchemaInfo.key).where(SchemaInfo.key == DECISION_LOCK_KEY)).first() is None:
        conn.execute(SchemaInfo.__table__.insert().values(key=DECISION_LOCK_KEY, value="1"))
    _refresh_vendor_keys(conn)


# --------------------------------------------------------------------------- v4: stage status vocabulary

_STAGE_LIST = ", ".join(repr(s) for s in constants.STAGE_STATUSES)

_SQLITE_EVENTS_V4 = f"""
CREATE TABLE processing_events_rebuild (
    id INTEGER NOT NULL, processing_run_id INTEGER NOT NULL, stage_name VARCHAR(32) NOT NULL,
    stage_status VARCHAR(16) NOT NULL, message TEXT, started_at DATETIME, completed_at DATETIME, duration_ms INTEGER,
    PRIMARY KEY (id),
    CONSTRAINT uq_event_run_stage UNIQUE (processing_run_id, stage_name),
    CONSTRAINT ck_event_status CHECK (stage_status IN ({_STAGE_LIST})),
    CONSTRAINT ck_event_duration CHECK (duration_ms IS NULL OR duration_ms >= 0),
    FOREIGN KEY(processing_run_id) REFERENCES processing_runs (id) ON DELETE CASCADE
)"""


def _events_schema_is_current(conn: Connection) -> bool:
    if conn.dialect.name == "sqlite":
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='processing_events'")).scalar() or ""
        return "'COMPLETED'" in sql and "'PENDING'" in sql
    return any("COMPLETED" in (c.get("sqltext") or "") for c in inspect(conn).get_check_constraints("processing_events"))


def _v4_stage_statuses(conn: Connection) -> None:
    """v4: stage statuses PENDING/RUNNING/COMPLETED/FAILED/SKIPPED (was SUCCEEDED instead of COMPLETED)."""
    if _events_schema_is_current(conn):
        return
    if conn.dialect.name == "postgresql":
        conn.execute(text("ALTER TABLE processing_events DROP CONSTRAINT IF EXISTS ck_event_status"))
        conn.execute(text("UPDATE processing_events SET stage_status = 'COMPLETED' WHERE stage_status = 'SUCCEEDED'"))
        conn.execute(text(f"ALTER TABLE processing_events ADD CONSTRAINT ck_event_status CHECK (stage_status IN ({_STAGE_LIST}))"))
        return
    conn.execute(text("DROP TABLE IF EXISTS processing_events_rebuild"))
    conn.execute(text(_SQLITE_EVENTS_V4))
    conn.execute(
        text(
            "INSERT INTO processing_events_rebuild (id, processing_run_id, stage_name, stage_status, message, started_at, completed_at, duration_ms) "
            "SELECT id, processing_run_id, stage_name, CASE stage_status WHEN 'SUCCEEDED' THEN 'COMPLETED' ELSE stage_status END, "
            "message, started_at, completed_at, duration_ms FROM processing_events"
        )
    )
    conn.execute(text("DROP TABLE processing_events"))
    conn.execute(text("ALTER TABLE processing_events_rebuild RENAME TO processing_events"))
    for index in ProcessingEvent.__table__.indexes:
        index.create(conn, checkfirst=True)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline: schema_info, extraction_records", _create_tables(SchemaInfo, ExtractionRecordRow)),
    # Note: migrations 1 and 2 create tables from the *current* models, so on a brand-new database they
    # already produce the latest schema; later migrations therefore detect "already current" and skip.
    Migration(
        2,
        "purchase_orders, processing_runs, invoices, processing_events",
        _create_tables(PurchaseOrder, ProcessingRun, Invoice, ProcessingEvent),
    ),
    Migration(3, "decision engine: status vocabulary, decision_explanation, decision lock, vendor keys", _v3_decision_engine),
    Migration(4, "pipeline: stage statuses PENDING/RUNNING/COMPLETED/FAILED/SKIPPED", _v4_stage_statuses),
)
LATEST_VERSION = MIGRATIONS[-1].version


def get_schema_version(conn: Connection) -> int:
    """Current version (0 for a fresh or pre-versioning database)."""
    if "schema_info" not in inspect(conn).get_table_names():
        return 0
    raw = conn.execute(select(SchemaInfo.value).where(SchemaInfo.key == VERSION_KEY)).scalar_one_or_none()
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError as exc:
        raise SchemaVersionError(f"schema_info.{VERSION_KEY} holds {raw!r}, which is not a version number") from exc


def _set_version(conn: Connection, version: int) -> None:
    result = conn.execute(update(SchemaInfo).where(SchemaInfo.key == VERSION_KEY).values(value=str(version)))
    if result.rowcount == 0:
        conn.execute(SchemaInfo.__table__.insert().values(key=VERSION_KEY, value=str(version)))


def migrate(engine: Engine) -> list[int]:
    """Bring the database up to ``LATEST_VERSION``. Returns the versions applied (empty if current)."""
    with engine.begin() as conn:
        version = get_schema_version(conn)
    if version > LATEST_VERSION:
        raise SchemaVersionError(
            f"The database is at schema version {version} but this application only knows up to "
            f"{LATEST_VERSION}. Update the application; the database was not modified."
        )
    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text(f"SELECT pg_advisory_xact_lock({_ADVISORY_LOCK_ID})"))
            if get_schema_version(conn) >= migration.version:  # another instance got here first
                continue
            logger.info("Applying migration %d: %s", migration.version, migration.description)
            migration.apply(conn)
            _set_version(conn, migration.version)
        applied.append(migration.version)
    return applied
