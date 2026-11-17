"""Exceptions for the ingestion layer. ``str(exc)`` is always safe and actionable to show a user."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoiceflow.schemas import ExtractionResult


class AiError(Exception):
    """Base for Gemini failures. ``str(exc)`` is user-safe and never contains the API key.

    ``retryable`` says whether the client may retry within its small attempt budget.
    ``unavailable`` distinguishes "the AI service cannot be used right now" (offer manual entry)
    from "the AI answered but the answer was unusable".
    """

    code = "ai_error"
    retryable = False
    unavailable = True

    def __init__(self, message: str, *, raw_response: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.retry_after = retry_after
        self.attempts = 0  # set by the client: how many API calls were made before giving up


class GeminiConfigError(AiError):
    code = "config_error"  # no/obviously malformed API key, unknown model


class GeminiAuthError(AiError):
    code = "auth_error"  # key rejected / no permission


class GeminiRateLimitError(AiError):
    code = "rate_limited"
    retryable = True


class GeminiUnavailableError(AiError):
    code = "service_unavailable"  # 5xx, timeouts, network
    retryable = True


class GeminiRequestError(AiError):
    code = "bad_request"  # other 4xx: our request was rejected
    unavailable = False


class GeminiBlockedError(AiError):
    code = "blocked"  # safety / policy block: retrying will not help
    unavailable = False


class GeminiInvalidResponseError(AiError):
    code = "invalid_response"  # empty, truncated, not JSON, or failed schema validation
    retryable = True
    unavailable = False


class IngestionError(Exception):
    """Base class. ``code`` is a stable machine-readable identifier for logs/UI."""

    code = "ingestion_error"


class PdfValidationError(IngestionError):
    """The upload was rejected before any processing (wrong type, too big, corrupt, ...)."""

    def __init__(self, message: str, code: str = "invalid_pdf") -> None:
        super().__init__(message)
        self.code = code


class ExtractionError(IngestionError):
    """The PDF was valid but text could not be extracted."""

    code = "extraction_failed"


class NoTextExtractedError(ExtractionError):
    """Neither the text layer nor OCR produced meaningful text."""

    code = "no_text"


class OcrUnavailableError(ExtractionError):
    """Pages need OCR but Tesseract is missing or unusable.

    ``partial_result`` holds whatever the text layer did yield, so a UI can show it honestly
    instead of pretending the document was fully read.
    """

    code = "ocr_unavailable"

    def __init__(self, message: str, partial_result: ExtractionResult | None = None) -> None:
        super().__init__(message)
        self.partial_result = partial_result
