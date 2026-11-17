"""The invoice pipeline: one uploaded PDF from first byte to saved decision.

    file hash -> validate PDF -> text layer -> OCR (if needed) -> AI extraction -> validate structure
    -> normalise -> duplicates -> PO matching -> financial validation -> decision -> save

Guarantees
* **Honest stages.** Every stage is a row in ``processing_events``. A stage is ``COMPLETED`` only after
  its work succeeded, ``FAILED`` if it raised, ``SKIPPED`` if it did not need to (or could not) run.
  Finding a duplicate or a mismatch is a *result*; the stage still COMPLETED. FAILED means "could not
  execute".
* **No lost history.** Stages before the decision commit one by one (a live view can follow them), and
  the AI answer is stored in ``extraction_records`` as soon as it exists. If a later stage fails, the
  failed stage, what was already extracted and the earlier events all stay in the database.
* **No approval from a failure.** A failed run saves a ``PROCESSING_FAILED`` invoice, never ``APPROVED``.
* **Consistent balances.** The duplicate/PO/validation/decision/save stages run in ONE transaction that
  holds the decision lock (see ``decision_service``), and their events are written in it, so a rollback
  can never leave "completed" events for work that was not saved.

``InvoicePipeline.process`` does not raise for expected problems (bad upload, AI outage, missing OCR);
it returns a ``PipelineResult`` with an actionable ``error``. The one exception: if the database itself
is unreachable when the run is created there is nowhere to record anything, so
``DatabaseUnavailableError`` propagates.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from invoiceflow import constants
from invoiceflow.ai.invoice_extractor import AiCallResult, build_record, record_from_manual
from invoiceflow.ai.provider import AiProvider, GeminiProvider
from invoiceflow.config import Settings, load_settings
from invoiceflow.database import repositories as repo
from invoiceflow.database.connection import DatabaseUnavailableError, get_engine, safe_error_reason, session_scope
from invoiceflow.database.types import utcnow
from invoiceflow.exceptions import ExtractionError, NoTextExtractedError, OcrUnavailableError, PdfValidationError
from invoiceflow.extraction import pdf_extractor
from invoiceflow.schemas import (
    AiExtractionRecord,
    AiInfo,
    CheckResult,
    DecisionConfig,
    DecisionResult,
    ExtractionResult,
    InvoiceExtraction,
    NormalizedInvoice,
    OcrInfo,
    PipelineError,
    PipelineResult,
    PoMatchingInfo,
    StageEvent,
)
from invoiceflow.services import decision_service as svc
from invoiceflow.services.validation_engine import DecisionBuilder
from invoiceflow.utils.pdf_security import PdfInfo, validate_pdf_upload

logger = logging.getLogger(__name__)

EventCallback = Callable[[StageEvent], None]

_TITLES = dict(constants.PIPELINE_STAGES)
_PO_CHECKS = frozenset(
    {"po_reference_present", "po_match_unambiguous", "po_found", "vendor_matches_po", "po_is_open", "currency_matches_po"}
)
_VALIDATION_CHECKS = frozenset(
    {
        "not_duplicate_file",
        "not_duplicate_invoice_number",
        "invoice_number_present",
        "total_present",
        "line_items_present",
        "line_arithmetic",
        "subtotal_matches_lines",
        "total_adds_up",
        "prior_invoices_reliable",
        "within_po_limit",
        "extraction_certain",
    }
)

# What the user can do about each error code.
ACTIONS = {
    "bad_extension": "Upload a file whose name ends in .pdf.",
    "bad_content_type": "Upload a PDF document.",
    "bad_signature": "This is not a real PDF, whatever its name says. Export or scan the invoice as a PDF and upload that.",
    "empty_file": "The file is empty. Upload the invoice PDF again.",
    "file_too_large": "Upload a smaller PDF (compress it or remove unneeded pages).",
    "too_many_pages": "Upload only the invoice pages, or split the document and upload each invoice separately.",
    "corrupt_pdf": "The file is damaged. Re-export or re-scan the invoice and upload it again.",
    "encrypted": "Remove the password protection and upload the PDF again.",
    "no_pages": "The PDF has no pages. Upload a valid invoice PDF.",
    "ocr_unavailable": "Install Tesseract OCR (or set TESSERACT_CMD), then upload again; or upload a PDF that has a text layer.",
    "no_text": "No text could be read. Upload a clearer or higher-resolution copy of the invoice.",
    "extraction_failed": "The document could not be read. Try again, or upload a different copy.",
    "config_error": "Add a valid GEMINI_API_KEY (and check GEMINI_MODEL), or enter the invoice fields manually.",
    "auth_error": "Gemini rejected the API key. Check GEMINI_API_KEY, or enter the invoice fields manually.",
    "rate_limited": "The Gemini quota or rate limit was reached. Wait a minute and try again, or enter the fields manually.",
    "service_unavailable": "Gemini is temporarily unavailable. Try again shortly, or enter the invoice fields manually.",
    "invalid_response": "The AI answer could not be used. Try again, or enter the invoice fields manually.",
    "blocked": "The AI service declined to process this document. Enter the invoice fields manually.",
    "bad_request": "The AI service rejected the request. Try again, or enter the invoice fields manually.",
    "empty_document": "There was no text to send to the AI. Upload a readable invoice.",
    "empty_extraction": "No invoice data could be found in the document. Check that it is an invoice, or enter the fields manually.",
    "internal_error": "An unexpected error occurred. The run was saved as failed; check the application log and try again.",
}
_MANUAL_OK = frozenset(
    {"config_error", "auth_error", "rate_limited", "service_unavailable", "invalid_response", "blocked", "bad_request", "empty_extraction"}
)


# ============================================================================= stage bookkeeping


class StageFailure(Exception):
    """A stage could not be executed. Carries everything needed for an actionable error."""

    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(message)
        self.stage, self.code, self.message = stage, code, message

    def to_error(self) -> PipelineError:
        return PipelineError(
            stage=self.stage,
            code=self.code,
            message=self.message,
            action=ACTIONS.get(self.code, ACTIONS["internal_error"]),
            manual_entry_possible=self.code in _MANUAL_OK,
        )


class StageSkipped(Exception):
    """Raised inside a stage body when the stage legitimately did not need to run."""


class _Handle:
    def __init__(self) -> None:
        self.message: str | None = None


class StageTracker:
    """Keeps every stage's real state in memory and mirrors it into ``processing_events``.

    ``session=None`` persists each change in its own committed transaction (visible to a live view
    immediately). Passing a session writes inside that transaction instead (used by the locked decision
    block). ``on_event`` is told about every change, so a UI can update as the pipeline runs.
    """

    def __init__(self, engine: Engine, run_id: str, on_event: EventCallback | None = None) -> None:
        self.engine, self.run_id, self._on_event = engine, run_id, on_event
        self.events: dict[str, StageEvent] = {n: StageEvent(stage_name=n, title=t) for n, t in constants.PIPELINE_STAGES}
        self._t0: dict[str, float] = {}

    def ordered(self) -> list[StageEvent]:
        return [self.events[n] for n, _ in constants.PIPELINE_STAGES]

    # -- persistence ---------------------------------------------------------
    def _write(self, fn: Callable[[Session], None], session: Session | None, *, best_effort: bool = False) -> None:
        """Persist a change. ``best_effort`` is for recording failures: if the database itself is the problem,
        log it and carry on, so reporting an error can never raise a second one."""
        try:
            if session is not None:
                fn(session)
            else:
                with session_scope(self.engine) as s:
                    fn(s)
        except Exception:
            if not best_effort:
                raise
            logger.exception("Could not persist a stage event for run %s", self.run_id)

    def _emit(self, ev: StageEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(ev.model_copy())
        except Exception:
            logger.exception("Stage event callback failed")

    def plan(self, session: Session) -> None:
        """Record every stage as PENDING so the whole plan is visible from the start."""
        for ev in self.events.values():
            repo.update_stage(session, self.run_id, ev.stage_name, constants.STAGE_PENDING)
        for ev in self.ordered():
            self._emit(ev)

    # -- transitions ---------------------------------------------------------
    def start(self, name: str, session: Session | None = None) -> None:
        ev = self.events[name]
        ev.status, ev.started_at, ev.completed_at, ev.duration_ms, ev.message = "RUNNING", utcnow(), None, None, None
        self._t0[name] = time.perf_counter()
        self._write(lambda s: repo.update_stage(s, self.run_id, name, constants.STAGE_RUNNING, at=ev.started_at), session)
        self._emit(ev)

    def _finish(self, name: str, status: str, message: str | None, session: Session | None) -> None:
        ev = self.events[name]
        now = utcnow()
        if ev.started_at is None:  # never started (skipped before running)
            ev.completed_at, ev.duration_ms = now, 0
        else:
            ev.completed_at = now
            ev.duration_ms = max(0, int((time.perf_counter() - self._t0.get(name, time.perf_counter())) * 1000))
        ev.status, ev.message = status, message  # type: ignore[assignment]
        self._write(
            lambda s: repo.update_stage(s, self.run_id, name, status, message, at=ev.completed_at, duration_ms=ev.duration_ms),
            session,
            best_effort=(status == constants.STAGE_FAILED and session is None),
        )
        self._emit(ev)

    def complete(self, name: str, message: str | None, session: Session | None = None) -> None:
        self._finish(name, constants.STAGE_COMPLETED, message, session)

    def fail(self, name: str, message: str, session: Session | None = None) -> None:
        self._finish(name, constants.STAGE_FAILED, message, session)

    def skip(self, name: str, message: str, session: Session | None = None) -> None:
        self._finish(name, constants.STAGE_SKIPPED, message, session)

    def record_measured(self, name: str, started_at: datetime, completed_at: datetime, duration_ms: int, message: str) -> None:
        """Store a stage that finished before the run row existed (hashing), with its real timings."""
        ev = self.events[name]
        ev.status, ev.started_at = "RUNNING", started_at
        self._emit(ev)  # announce it as the live view would have seen it, with its real start time
        ev.status, ev.completed_at, ev.duration_ms, ev.message = "COMPLETED", completed_at, duration_ms, message
        self._write(lambda s: self._persist_full(s, ev), None)
        self._emit(ev)

    def skip_remaining(self, reason: str, session: Session, *, except_stages: tuple[str, ...] = ()) -> None:
        for ev in self.ordered():
            if ev.status == "PENDING" and ev.stage_name not in except_stages:
                ev.status, ev.message, ev.completed_at, ev.duration_ms = "SKIPPED", reason, utcnow(), 0
                repo.update_stage(session, self.run_id, ev.stage_name, constants.STAGE_SKIPPED, reason, at=ev.completed_at, duration_ms=0)
                self._emit(ev)

    def _persist_full(self, session: Session, ev: StageEvent) -> None:
        """Write an event's complete in-memory state (used when an earlier write was rolled back)."""
        if ev.status == "PENDING":
            repo.update_stage(session, self.run_id, ev.stage_name, constants.STAGE_PENDING)
            return
        if ev.started_at is not None:
            repo.update_stage(session, self.run_id, ev.stage_name, constants.STAGE_RUNNING, at=ev.started_at)
        if ev.status != "RUNNING":
            repo.update_stage(session, self.run_id, ev.stage_name, ev.status, ev.message, at=ev.completed_at, duration_ms=ev.duration_ms)

    def sync_all(self, session: Session) -> None:
        for ev in self.ordered():
            self._persist_full(session, ev)


# ============================================================================= pipeline


@dataclass
class _Context:
    """Everything learned so far; the source for both the saved failure record and the final result."""

    run_id: str
    filename: str
    file_hash: str
    started_at: datetime
    t0: float
    tracker: StageTracker
    manual: InvoiceExtraction | None
    info: PdfInfo | None = None
    layer: pdf_extractor.TextLayer | None = None
    ingestion: ExtractionResult | None = None
    ocr_needed: list[int] = field(default_factory=list)
    call: AiCallResult | None = None
    extraction: InvoiceExtraction | None = None
    record: AiExtractionRecord | None = None
    normalized: NormalizedInvoice | None = None
    flags: list[str] = field(default_factory=list)
    decision: DecisionResult | None = None
    failure: StageFailure | None = None
    persistence_error: str | None = None
    completed_at: datetime | None = None
    run_status: str = constants.RUN_RUNNING


class InvoicePipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        engine: Engine | None = None,
        ai_provider: AiProvider | None = None,
        config: DecisionConfig | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self._engine = engine
        self.provider: AiProvider = ai_provider or GeminiProvider(self.settings)
        _refuse_test_double_outside_tests(self.provider)
        self.config = config or svc.config_from_settings(self.settings)

    @property
    def engine(self) -> Engine:
        return self._engine or get_engine()

    # ------------------------------------------------------------------ public API

    def process(
        self,
        data: bytes,
        filename: str | None = None,
        *,
        content_type: str | None = None,
        manual_extraction: InvoiceExtraction | None = None,
        on_event: EventCallback | None = None,
    ) -> PipelineResult:
        """Run one upload through every stage and return the complete, persisted result.

        ``manual_extraction`` replaces the AI stage with fields entered by a person (the way forward when
        the AI is unavailable); it goes through the same normalisation, checks and decision rules.

        ``on_event`` is called (in this thread) for every stage change so a UI can show live progress. During
        the last five stages the pipeline holds a database write lock, so the callback must not use the
        database itself (on SQLite it would wait for a lock held by its own thread); just update the screen.
        """
        t0 = time.perf_counter()
        started_at = utcnow()
        # The run row needs the file hash, so hashing happens first; its event keeps its real timings.
        file_hash = hashlib.sha256(data).hexdigest()
        hash_ms = int((time.perf_counter() - t0) * 1000)
        hash_done = utcnow()

        try:
            with session_scope(self.engine) as s:  # if this fails there is nowhere to record anything
                run = repo.create_processing_run(s, filename, file_hash)
                tracker = StageTracker(self.engine, run.run_id, on_event)
                tracker.plan(s)
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                f"The run could not be started because the database is not usable: {safe_error_reason(exc, self.engine)}"
            ) from None
        ctx = _Context(run.run_id, run.original_filename, file_hash, started_at, t0, tracker, manual_extraction)
        tracker.record_measured(
            constants.STAGE_FILE_HASH,
            started_at,
            hash_done,
            hash_ms,
            f"SHA-256 {file_hash[:12]}… of {len(data):,} bytes",
        )

        try:
            self._intake(ctx, data, filename, content_type)
            self._extract_with_ai(ctx)
            self._decide_and_save(ctx)
        except StageFailure as failure:
            ctx.failure = failure
        except Exception as exc:
            logger.exception("Unexpected error in pipeline run %s", ctx.run_id)
            # Blame the stage that was running, else the next one that had not started yet, else (every stage had
            # already reported success) the final save, whose transaction must have failed at COMMIT. That stage
            # was marked COMPLETED before its commit succeeded, so it is corrected to FAILED here.
            ordered = tracker.ordered()
            current = next((e.stage_name for e in ordered if e.status == "RUNNING"), None) or next(
                (e.stage_name for e in ordered if e.status == "PENDING"), constants.STAGE_SAVE
            )
            what = "The database rejected the change" if isinstance(exc, SQLAlchemyError) else "Unexpected error"
            tracker.fail(current, f"{what} ({type(exc).__name__})")
            ctx.failure = StageFailure(current, "internal_error", f"{what} ({type(exc).__name__}); see the application log.")
        if ctx.failure is not None:
            self._record_failure(ctx)
        return self._build_result(ctx)

    # ------------------------------------------------------------------ stage helper

    @contextmanager
    def _stage(self, ctx: _Context, name: str, session: Session | None = None) -> Iterator[_Handle]:
        tracker = ctx.tracker
        tracker.start(name, session)
        handle = _Handle()
        try:
            yield handle
        except StageSkipped as skip:
            tracker.skip(name, str(skip), session)
        except StageFailure as failure:
            tracker.fail(name, failure.message, session)
            raise
        except Exception as exc:
            logger.exception("Stage %s raised", name)
            message = f"Unexpected error ({type(exc).__name__})"
            tracker.fail(name, message, session)
            raise StageFailure(name, "internal_error", message) from exc
        else:
            tracker.complete(name, handle.message, session)

    # ------------------------------------------------------------------ stages 2-4: the document

    def _intake(self, ctx: _Context, data: bytes, filename: str | None, content_type: str | None) -> None:
        with self._stage(ctx, constants.STAGE_VALIDATION) as st:
            try:
                ctx.info = validate_pdf_upload(
                    data,
                    filename,
                    max_bytes=self.settings.max_upload_bytes,
                    max_pages=self.settings.max_pdf_pages,
                    content_type=content_type,
                )
            except PdfValidationError as exc:
                raise StageFailure(constants.STAGE_VALIDATION, exc.code, str(exc)) from exc
            warn = f", {len(ctx.info.warnings)} warning(s)" if ctx.info.warnings else ""
            st.message = f"Valid PDF: {ctx.info.page_count} page(s), {ctx.info.size_bytes:,} bytes{warn}."

        with self._stage(ctx, constants.STAGE_TEXT) as st:
            try:
                ctx.layer = pdf_extractor.read_text_layer(data, ctx.info)  # type: ignore[arg-type]
                ctx.ocr_needed = pdf_extractor.pages_needing_ocr(ctx.layer)
                chars = sum(p.text_layer_chars for p in ctx.layer.pages)
                if not ctx.ocr_needed:
                    ctx.ingestion = pdf_extractor.finalize(ctx.layer)
            except NoTextExtractedError as exc:
                raise StageFailure(constants.STAGE_TEXT, exc.code, str(exc)) from exc
            except ExtractionError as exc:
                raise StageFailure(constants.STAGE_TEXT, exc.code, str(exc)) from exc
            need = f"; page(s) {', '.join(map(str, ctx.ocr_needed))} have no usable text layer" if ctx.ocr_needed else ""
            st.message = f"Read {len(ctx.layer.pages)} page(s), {chars:,} text characters{need}."

        with self._stage(ctx, constants.STAGE_OCR) as st:
            if not ctx.ocr_needed:
                raise StageSkipped("Every page has a usable text layer, so OCR was not needed.")
            try:
                pdf_extractor.run_ocr(ctx.layer, self.settings)  # type: ignore[arg-type]
                ctx.ingestion = pdf_extractor.finalize(ctx.layer)  # type: ignore[arg-type]
            except OcrUnavailableError as exc:
                ctx.ingestion = exc.partial_result  # keep what the text layer did give us
                raise StageFailure(constants.STAGE_OCR, exc.code, str(exc)) from exc
            except (NoTextExtractedError, ExtractionError) as exc:
                raise StageFailure(constants.STAGE_OCR, exc.code, str(exc)) from exc
            confs = [p.ocr_confidence for p in ctx.ingestion.pages if p.ocr_confidence is not None]
            avg = f", average confidence {sum(confs) / len(confs):.0f}/100" if confs else ""
            st.message = f"OCR read page(s) {', '.join(map(str, ctx.ingestion.ocr_pages)) or '(none useful)'}{avg}."

    # ------------------------------------------------------------------ stages 5-7: the invoice data

    def _extract_with_ai(self, ctx: _Context) -> None:
        assert ctx.ingestion is not None
        if ctx.manual is not None:
            with self._stage(ctx, constants.STAGE_AI) as st:
                raise StageSkipped("Manual entry was supplied, so the AI was not used.")
            ctx.extraction = ctx.manual
        else:
            with self._stage(ctx, constants.STAGE_AI) as st:
                ctx.call = self.provider.extract(ctx.ingestion.text, source_method=ctx.ingestion.method)
                if ctx.call.status != "success" or ctx.call.extraction is None:
                    raise StageFailure(
                        constants.STAGE_AI,
                        ctx.call.error_code or "invalid_response",
                        ctx.call.error_message or "The AI returned no extraction.",
                    )
                ctx.extraction = ctx.call.extraction
                st.message = f"{ctx.call.provider} ({ctx.call.model}) answered after {ctx.call.attempts} call(s)."

        with self._stage(ctx, constants.STAGE_STRUCTURE) as st:
            ex = ctx.extraction
            assert ex is not None
            InvoiceExtraction.model_validate(ex.model_dump())  # schema round-trip
            has_content = bool(ex.invoice_number or ex.vendor_name or ex.grand_total or ex.subtotal or ex.line_items)
            if not has_content:
                raise StageFailure(
                    constants.STAGE_STRUCTURE,
                    "empty_extraction",
                    "The extraction is empty: no invoice number, vendor, amounts or line items were found.",
                )
            st.message = f"Valid structure: {len(ex.line_items)} line item(s), {len(ex.evidence)} evidence snippet(s)."

        with self._stage(ctx, constants.STAGE_NORMALIZE) as st:
            if ctx.manual is not None:
                ctx.record = record_from_manual(ctx.manual, source_text=ctx.ingestion.text, file_sha256=ctx.file_hash)
            else:
                ctx.record = build_record(ctx.call, ctx.file_hash)  # type: ignore[arg-type]
            with session_scope(self.engine) as s:  # keep the audit record even if a later stage fails
                repo.save_extraction_record(s, ctx.record)
            ctx.normalized = ctx.record.normalized
            ctx.flags = svc.uncertainty_flags_from_record(ctx.record, _ingestion_flags(ctx.ingestion))
            st.message = f"{len(ctx.record.warnings)} warning(s); {len(ctx.flags)} value(s) flagged as uncertain."

    # ------------------------------------------------------------------ stages 8-12: decision (one transaction)

    def _decide_and_save(self, ctx: _Context) -> None:
        assert ctx.normalized is not None and ctx.record is not None
        with session_scope(self.engine) as s:
            repo.acquire_decision_lock(s)  # held until this transaction commits
            data = svc.gather_decision_input(s, ctx.run_id, ctx.normalized, ctx.file_hash, self.config, ctx.flags)
            builder = DecisionBuilder(data, self.config)

            with self._stage(ctx, constants.STAGE_DUPLICATES, s) as st:
                found = [c for c in builder.duplicate_checks() if c.reason_code in ("DUPLICATE_FILE", "DUPLICATE_INVOICE_NUMBER")]
                st.message = (
                    "Duplicate found: " + "; ".join(c.detail for c in found) if found else "No duplicate of this file or invoice number."
                )
            with self._stage(ctx, constants.STAGE_PO, s) as st:
                out = builder.po_checks()
                bad = [c for c in out if c.reason_code]
                st.message = (
                    ("PO problem: " + "; ".join(c.detail for c in bad))
                    if bad
                    else (
                        f"Matched open purchase order {data.po.po_number} (vendor and currency agree)."
                        if data.po
                        else "No purchase order to match."
                    )
                )
            with self._stage(ctx, constants.STAGE_FINANCIAL, s) as st:
                out = builder.financial_checks()
                bad = [c for c in out if c.reason_code]
                st.message = (
                    f"{len(bad)} financial problem(s): " + "; ".join(c.detail for c in bad)
                    if bad
                    else f"All {len(out)} financial checks passed."
                )
            with self._stage(ctx, constants.STAGE_DECISION, s) as st:
                ctx.decision = builder.finish()
                n = len(ctx.decision.reasons)
                st.message = f"{ctx.decision.status}" + (f" ({n} reason{'s' if n != 1 else ''})" if n else "")
            with self._stage(ctx, constants.STAGE_SAVE, s) as st:
                repo.save_invoice(
                    s,
                    ctx.run_id,
                    ctx.normalized,
                    final_status=ctx.decision.status,
                    decision_reasons=ctx.decision.reason_codes,
                    extraction_json=_preserved_json(ctx),
                    decision_explanation=ctx.decision.model_dump(mode="json"),
                )
                repo.complete_run(s, ctx.run_id, constants.RUN_COMPLETED)
                st.message = f"Saved invoice as {ctx.decision.status}."
        ctx.run_status, ctx.completed_at = constants.RUN_COMPLETED, utcnow()
        logger.info("Run %s finished: %s", ctx.run_id[:8], ctx.decision.status)

    # ------------------------------------------------------------------ failure handling

    def _record_failure(self, ctx: _Context) -> None:
        """Persist the failure without losing anything: events, AI record, extracted data, failed invoice, failed run."""
        failure = ctx.failure
        assert failure is not None
        tracker = ctx.tracker
        ctx.decision = None
        ctx.run_status, ctx.completed_at = constants.RUN_FAILED, utcnow()
        try:
            if ctx.call is not None and ctx.record is None:  # AI answered (or failed) but never reached normalisation
                ctx.record = _failure_record(ctx)
            if ctx.record is not None:  # keep the AI audit record even if the main failure record cannot be written
                try:
                    with session_scope(self.engine) as s:
                        repo.save_extraction_record(s, ctx.record)
                except Exception:
                    logger.exception("Could not save the extraction record for failed run %s", ctx.run_id)
            with session_scope(self.engine) as s:
                tracker.skip_remaining(
                    f"Not run because {_TITLES.get(failure.stage, failure.stage).lower()} failed.", s, except_stages=(constants.STAGE_SAVE,)
                )
                result = svc.record_processing_failure(
                    s,
                    ctx.run_id,
                    failure.message,
                    normalized=ctx.normalized,
                    extraction_json=_preserved_json(ctx),
                )
                ctx.decision = result
                if tracker.events[constants.STAGE_SAVE].status == "PENDING":  # the failure record itself is what got saved
                    tracker.complete(constants.STAGE_SAVE, "Saved the failed run as PROCESSING_FAILED; earlier results were kept.", s)
                tracker.sync_all(s)  # re-assert every stage's true state (an earlier write may have been rolled back)
                repo.complete_run(s, ctx.run_id, constants.RUN_FAILED, f"{_TITLES.get(failure.stage, failure.stage)}: {failure.message}")
        except Exception as exc:
            logger.exception("Could not save the failed run %s", ctx.run_id)
            ctx.persistence_error = f"The failure could not be saved ({type(exc).__name__}). Check the database connection."

    # ------------------------------------------------------------------ result

    def _build_result(self, ctx: _Context) -> PipelineResult:
        decision = ctx.decision
        po_info: PoMatchingInfo | None = None
        validation: list[CheckResult] = []
        if decision is not None and ctx.failure is None:
            allc = decision.checks_passed + decision.checks_failed + decision.checks_skipped
            po_info = PoMatchingInfo(
                matched_po_number=decision.matched_po_number,
                po_amount=decision.po_amount,
                already_approved_amount=decision.already_approved_amount,
                remaining_balance=decision.remaining_balance,
                suggestions=decision.po_suggestions,
                checks=[c for c in allc if c.check in _PO_CHECKS],
            )
            validation = [c for c in allc if c.check in _VALIDATION_CHECKS]
        ing = ctx.ingestion
        ocr = None
        if ctx.layer is not None or ing is not None:
            pages = (ing.pages if ing else ctx.layer.pages) if (ing or ctx.layer) else []
            ocr_pages = [p.page_number for p in pages if p.ocr_attempted] or ctx.ocr_needed
            ocr = OcrInfo(
                needed=bool(ctx.ocr_needed),
                pages=ocr_pages,
                performed=any(p.ocr_attempted for p in pages),
                confidences={p.page_number: p.ocr_confidence for p in pages if p.ocr_attempted},
                text_method=ing.method if ing else None,
                warnings=[w for p in pages for w in p.warnings if "OCR" in w],
            )
        duration = None
        if ctx.completed_at:
            duration = round(time.perf_counter() - ctx.t0, 3)
        return PipelineResult(
            run_id=ctx.run_id,
            run_status=ctx.run_status,  # type: ignore[arg-type]
            filename=ctx.filename,
            file_hash=ctx.file_hash,
            started_at=ctx.started_at,
            completed_at=ctx.completed_at,
            duration_seconds=duration,
            invoice=ctx.normalized,
            ingestion=ing,
            ocr=ocr,
            ai=self._ai_info(ctx),
            po_matching=po_info,
            validation=validation,
            decision=decision,
            decision_status=decision.status if decision else None,
            decision_reasons=list(decision.reasons) if decision else [],
            stages=ctx.tracker.ordered(),
            error=ctx.failure.to_error() if ctx.failure else None,
            persistence_error=ctx.persistence_error,
        )

    def _ai_info(self, ctx: _Context) -> AiInfo:
        rec, call = ctx.record, ctx.call
        if ctx.manual is not None:
            return AiInfo(
                provider=None,
                used=False,
                source="manual",
                status="manual",
                record_id=rec.record_id if rec else None,
                warnings=list(rec.warnings) if rec else [],
                evidence=list(rec.evidence) if rec else [],
            )
        if call is None:
            return AiInfo(provider=self.provider.name, is_test_double=self.provider.is_test_double, used=False)
        return AiInfo(
            provider=call.provider,
            is_test_double=call.is_test_double,
            used=True,
            source="ai",
            status=rec.status if rec else call.status,
            model=call.model,
            attempts=call.attempts,
            record_id=rec.record_id if rec else None,
            error_code=call.error_code,
            error_message=call.error_message,
            warnings=list(rec.warnings) if rec else list(call.warnings),
            evidence=list(rec.evidence) if rec else [],
        )


# ============================================================================= helpers


def _failure_record(ctx: _Context) -> AiExtractionRecord:
    """The audit record for an AI attempt whose run then failed. Deliberately does not depend on normalisation:
    if the normaliser is what crashed, the model's raw answer must still be kept."""
    call = ctx.call
    assert call is not None
    try:
        return build_record(call, ctx.file_hash)
    except Exception:
        logger.exception("Could not build the extraction record normally; storing the raw answer instead")
        return AiExtractionRecord(
            source="ai",
            status="failed",
            model=call.model,
            attempts=call.attempts,
            file_sha256=ctx.file_hash,
            extraction=call.extraction,
            warnings=list(call.warnings),
            raw_response=call.raw_response,
            error_code=call.error_code or "processing_error",
            error_message=call.error_message or "The model answered, but the answer could not be processed.",
        )


def _refuse_test_double_outside_tests(provider: AiProvider) -> None:
    """A fake provider must never be mistaken for Gemini in the real application."""
    if getattr(provider, "is_test_double", False) and "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError(
            f"{type(provider).__name__} is a test double and can only be used by automated tests; the application must use GeminiProvider."
        )


def _ingestion_flags(ing: ExtractionResult) -> list[str]:
    """Reasons to distrust the document text itself: weak OCR, or a PDF that had to be repaired."""
    flags = [w.rstrip(".") for p in ing.pages for w in p.warnings if "OCR" in w]
    flags += [w.rstrip(".") for w in ing.warnings if "repaired" in w]
    return list(dict.fromkeys(flags))


def _ingestion_summary(ing: ExtractionResult | None, layer: pdf_extractor.TextLayer | None) -> dict | None:
    """Compact, text-free description of how the document was read (kept on failed runs)."""
    if ing is None and layer is None:
        return None
    pages = ing.pages if ing else layer.pages  # type: ignore[union-attr]
    return {
        "method": ing.method if ing else None,
        "page_count": len(pages),
        "ocr_pages": [p.page_number for p in pages if p.ocr_attempted],
        "pages": [
            {
                "page": p.page_number,
                "method": p.method,
                "text_chars": p.meaningful_chars,
                "ocr_confidence": p.ocr_confidence,
                "warnings": p.warnings,
            }
            for p in pages
        ],
        "warnings": ing.warnings if ing else [],
        "metadata": ing.metadata if ing else (layer.metadata if layer else {}),
    }


def _preserved_json(ctx: _Context) -> dict:
    """What is stored in ``invoices.extraction_json``: the data, and how it was obtained."""
    out: dict = {"ingestion": _ingestion_summary(ctx.ingestion, ctx.layer)}
    if ctx.normalized is not None:
        out["invoice"] = ctx.normalized.model_dump(mode="json")
    if ctx.record is not None:
        out["extraction_record_id"] = ctx.record.record_id
        out["source"] = ctx.record.source
    if ctx.call is not None and ctx.call.status != "success":
        out["ai_error"] = {
            "provider": ctx.call.provider,
            "model": ctx.call.model,
            "code": ctx.call.error_code,
            "message": ctx.call.error_message,
            "attempts": ctx.call.attempts,
        }
    if ctx.failure is not None:
        out["failed_stage"] = ctx.failure.stage
        out["failure_code"] = ctx.failure.code
    return out
