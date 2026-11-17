"""AI invoice extraction with an honest fallback.

``run_ai_extraction`` never raises for API problems: it returns an :class:`AiExtractionRecord`
whose ``status`` says exactly what happened (``success`` / ``unavailable`` / ``failed``), so the
app keeps running and can offer manual entry. Nothing is ever invented on failure.

The LLM only extracts. Its output goes through the deterministic normalizer; it makes no decision.
"""
from __future__ import annotations

import logging

from invoiceflow import constants
from invoiceflow.ai.gemini_client import GeminiClient, redact
from invoiceflow.ai.prompts import SYSTEM_INSTRUCTION, build_user_prompt
from invoiceflow.config import Settings
from invoiceflow.exceptions import AiError, GeminiConfigError
from invoiceflow.schemas import AiExtractionRecord, InvoiceExtraction
from invoiceflow.services.normalizer import audit_against_source, normalize_extraction

logger = logging.getLogger(__name__)

MAX_RAW_RESPONSE_CHARS = 200_000
MANUAL_NOTE = "Entered or corrected manually; not produced by AI extraction."


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_RAW_RESPONSE_CHARS else text[:MAX_RAW_RESPONSE_CHARS] + "\n[truncated]"


def run_ai_extraction(
    document_text: str,
    settings: Settings,
    *,
    source_method: str = "text",
    file_sha256: str | None = None,
    client: GeminiClient | None = None,
) -> AiExtractionRecord:
    """Extract invoice fields with Gemini. Always returns a record; see module docstring."""
    warnings: list[str] = []
    text = document_text
    if len(text) > constants.MAX_DOCUMENT_CHARS:
        text = text[: constants.MAX_DOCUMENT_CHARS]
        warnings.append(f"The document text was truncated to {constants.MAX_DOCUMENT_CHARS:,} characters before AI extraction.")
    if not text.strip():
        return _failed("empty_document", "There is no document text to extract from.", settings, file_sha256, status="failed")

    try:
        gemini = client or GeminiClient.from_settings(settings)
        result = gemini.generate_structured(
            system_instruction=SYSTEM_INSTRUCTION,
            contents=build_user_prompt(text, source_method=source_method),
            response_model=InvoiceExtraction,
        )
    except AiError as exc:
        logger.warning("AI extraction did not succeed: %s: %s", exc.code, exc)
        return _failed(
            exc.code, redact(str(exc), settings.gemini_api_key), settings, file_sha256,
            status="unavailable" if exc.unavailable else "failed", raw=exc.raw_response, warnings=warnings,
            attempts=exc.attempts,
        )

    extraction: InvoiceExtraction = result.parsed
    normalized, norm_warnings = normalize_extraction(extraction)
    evidence, audit_warnings = audit_against_source(extraction, normalized, text)
    return AiExtractionRecord(
        source="ai",
        status="success",
        model=result.model,
        attempts=result.attempts,
        file_sha256=file_sha256,
        extraction=extraction,
        normalized=normalized,
        evidence=evidence,
        warnings=warnings + norm_warnings + audit_warnings,
        raw_response=_truncate(result.raw_text),
    )


def _failed(
    code: str,
    message: str,
    settings: Settings,
    file_sha256: str | None,
    *,
    status: str,
    raw: str | None = None,
    warnings: list[str] | None = None,
    attempts: int = 0,
) -> AiExtractionRecord:
    return AiExtractionRecord(
        source="ai",
        status=status,  # type: ignore[arg-type]
        model=settings.gemini_model,
        attempts=attempts,
        file_sha256=file_sha256,
        warnings=list(warnings or []),
        raw_response=_truncate(raw),
        error_code=code,
        error_message=message,
    )


def record_from_manual(
    extraction: InvoiceExtraction,
    *,
    source_text: str | None = None,
    file_sha256: str | None = None,
    based_on: AiExtractionRecord | None = None,
) -> AiExtractionRecord:
    """Wrap manually entered / corrected fields in the same record type, via the same normalizer.

    ``based_on`` is the failed or AI record being corrected (kept only as a pointer in the warnings).
    Manual values are cross-checked against the document text when it is available.
    """
    normalized, warnings = normalize_extraction(extraction)
    evidence = []
    if source_text:
        evidence, audit_warnings = audit_against_source(extraction, normalized, source_text)
        warnings += audit_warnings
    notes = [MANUAL_NOTE]
    if based_on is not None:
        notes.append(f"Replaces extraction record {based_on.record_id} (status: {based_on.status}).")
    return AiExtractionRecord(
        source="manual",
        status="manual",
        model=None,
        file_sha256=file_sha256,
        extraction=extraction,
        normalized=normalized,
        evidence=evidence,
        warnings=notes + warnings,
    )


def unavailable_reason(settings: Settings) -> str | None:
    """Non-None when AI extraction cannot even be attempted (used by the UI before calling)."""
    try:
        from invoiceflow.ai.gemini_client import validate_api_key_format

        validate_api_key_format(settings.gemini_api_key)
    except GeminiConfigError as exc:
        return str(exc)
    return None
