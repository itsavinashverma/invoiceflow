"""AI invoice extraction with an honest fallback.

``run_ai_extraction`` never raises for API problems: it returns an :class:`AiExtractionRecord`
whose ``status`` says exactly what happened (``success`` / ``unavailable`` / ``failed``), so the
app keeps running and can offer manual entry. Nothing is ever invented on failure.

The LLM only extracts. Its output goes through the deterministic normalizer; it makes no decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

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


@dataclass
class AiCallResult:
    """Raw outcome of asking a model to extract fields, BEFORE normalisation and auditing.

    Produced by an :class:`~invoiceflow.ai.provider.AiProvider`. ``extraction`` is the model's output,
    already validated against the ``InvoiceExtraction`` schema, and is ``None`` unless ``status`` is
    ``"success"``. ``text_used`` is the (possibly truncated) document text that was sent.
    """

    provider: str
    model: str | None
    status: Literal["success", "unavailable", "failed"]
    extraction: InvoiceExtraction | None = None
    attempts: int = 0
    raw_response: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    text_used: str = ""
    is_test_double: bool = False


def call_gemini(
    document_text: str,
    settings: Settings,
    *,
    source_method: str = "text",
    client: GeminiClient | None = None,
) -> AiCallResult:
    """Ask Gemini to extract the invoice fields. Never raises for API problems; see :class:`AiCallResult`."""
    warnings: list[str] = []
    text = document_text
    if len(text) > constants.MAX_DOCUMENT_CHARS:
        text = text[: constants.MAX_DOCUMENT_CHARS]
        warnings.append(f"The document text was truncated to {constants.MAX_DOCUMENT_CHARS:,} characters before AI extraction.")
    base = dict(provider="gemini", model=settings.gemini_model, warnings=warnings, text_used=text)
    if not text.strip():
        return AiCallResult(
            status="failed", error_code="empty_document", error_message="There is no document text to extract from.", **base
        )
    try:
        gemini = client or GeminiClient.from_settings(settings)
        result = gemini.generate_structured(
            system_instruction=SYSTEM_INSTRUCTION,
            contents=build_user_prompt(text, source_method=source_method),
            response_model=InvoiceExtraction,
        )
    except AiError as exc:
        logger.warning("AI extraction did not succeed: %s: %s", exc.code, exc)
        return AiCallResult(
            status="unavailable" if exc.unavailable else "failed",
            attempts=exc.attempts,
            raw_response=_truncate(exc.raw_response),
            error_code=exc.code,
            error_message=redact(str(exc), settings.gemini_api_key),
            **base,
        )
    return AiCallResult(
        status="success",
        extraction=result.parsed,
        attempts=result.attempts,
        raw_response=_truncate(result.raw_text),
        **{**base, "model": result.model},
    )


def build_record(call: AiCallResult, file_sha256: str | None = None) -> AiExtractionRecord:
    """Normalise and audit a model answer into an :class:`AiExtractionRecord` (or record the failure)."""
    if call.status != "success" or call.extraction is None:
        return AiExtractionRecord(
            source="ai",
            status=call.status if call.status != "success" else "failed",
            model=call.model,
            attempts=call.attempts,
            file_sha256=file_sha256,
            warnings=list(call.warnings),
            raw_response=call.raw_response,
            error_code=call.error_code or "no_extraction",
            error_message=call.error_message or "The model returned no extraction.",
        )
    normalized, norm_warnings = normalize_extraction(call.extraction)
    evidence, audit_warnings = audit_against_source(call.extraction, normalized, call.text_used)
    return AiExtractionRecord(
        source="ai",
        status="success",
        model=call.model,
        attempts=call.attempts,
        file_sha256=file_sha256,
        extraction=call.extraction,
        normalized=normalized,
        evidence=evidence,
        warnings=list(call.warnings) + norm_warnings + audit_warnings,
        raw_response=call.raw_response,
    )


def run_ai_extraction(
    document_text: str,
    settings: Settings,
    *,
    source_method: str = "text",
    file_sha256: str | None = None,
    client: GeminiClient | None = None,
) -> AiExtractionRecord:
    """Extract invoice fields with Gemini. Always returns a record; see module docstring."""
    return build_record(call_gemini(document_text, settings, source_method=source_method, client=client), file_sha256)


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
