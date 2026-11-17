"""Housekeeping that keeps stored history truthful after crashes.

A run can be left ``RUNNING`` if the server is killed mid-run or the database drops out while the failure
itself is being recorded. ``fail_abandoned_runs`` closes such runs honestly: the run becomes ``FAILED``, its
unfinished stages are marked, and a ``PROCESSING_FAILED`` invoice record exists (never an approval).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow import constants
from invoiceflow.database import repositories as repo
from invoiceflow.database.models import Invoice, ProcessingEvent, ProcessingRun
from invoiceflow.database.types import utcnow
from invoiceflow.services.validation_engine import failed_result

logger = logging.getLogger(__name__)

ABANDONED_AFTER = timedelta(minutes=15)  # far longer than any real run (the AI client gives up within about 3 minutes)
INTERRUPTED = "The run was interrupted before it finished (the application stopped or lost its database connection)."


def fail_abandoned_runs(session: Session, *, older_than: timedelta = ABANDONED_AFTER, now: datetime | None = None) -> int:
    """Mark ``RUNNING`` runs older than ``older_than`` as failed. Returns how many were closed. Caller commits."""
    now = now or datetime.now(UTC)
    stale = list(
        session.scalars(
            select(ProcessingRun).where(ProcessingRun.run_status == constants.RUN_RUNNING, ProcessingRun.started_at <= now - older_than)
        )
    )
    for run in stale:
        done = utcnow()
        events = list(
            session.scalars(select(ProcessingEvent).where(ProcessingEvent.processing_run_id == run.id).order_by(ProcessingEvent.id))
        )
        for event in events:
            if event.stage_status == constants.STAGE_RUNNING:
                event.stage_status, event.message = constants.STAGE_FAILED, INTERRUPTED
                event.completed_at, event.duration_ms = done, max(0, int((done - (event.started_at or done)).total_seconds() * 1000))
            elif event.stage_status == constants.STAGE_PENDING:
                event.stage_status, event.message = constants.STAGE_SKIPPED, "Not run because the processing run was interrupted."
                event.completed_at, event.duration_ms = done, 0
        if not any(e.stage_status == constants.STAGE_FAILED for e in events):
            # Nothing was mid-flight when it stopped: the first stage that never ran is where the run ended.
            first_unrun = next((e for e in events if e.stage_status == constants.STAGE_SKIPPED), None)
            if first_unrun is not None:
                first_unrun.stage_status, first_unrun.message = constants.STAGE_FAILED, INTERRUPTED
        if session.scalars(select(Invoice).where(Invoice.processing_run_id == run.id)).first() is None:
            result = failed_result(INTERRUPTED)
            repo.save_invoice(
                session,
                run.run_id,
                None,
                final_status=result.status,
                decision_reasons=result.reason_codes,
                decision_explanation=result.model_dump(mode="json"),
            )
        run.run_status, run.completed_at, run.overall_error = constants.RUN_FAILED, done, INTERRUPTED
        logger.warning("Closed abandoned run %s", run.run_id)
    session.flush()
    return len(stale)
