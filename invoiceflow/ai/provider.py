"""The seam between the pipeline and whatever extracts invoice fields.

The pipeline depends only on :class:`AiProvider`. The user-facing application always uses
:class:`GeminiProvider`. Automated tests may substitute a *test double* (it lives under ``tests/``,
never in this package); a provider marked ``is_test_double`` is refused by the pipeline unless pytest
is running, and its results carry a model name and a warning that say so.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from invoiceflow.ai.gemini_client import GeminiClient
from invoiceflow.ai.invoice_extractor import AiCallResult, call_gemini
from invoiceflow.config import Settings


@runtime_checkable
class AiProvider(Protocol):
    name: str
    is_test_double: bool

    def extract(self, document_text: str, *, source_method: str) -> AiCallResult:
        """Return the model's structured answer or an honest failure. Must not raise for API problems."""
        ...


class GeminiProvider:
    """The real provider: Gemini through the official ``google-genai`` SDK."""

    name = "gemini"
    is_test_double = False

    def __init__(self, settings: Settings, client: GeminiClient | None = None) -> None:
        self._settings = settings
        self._client = client

    def extract(self, document_text: str, *, source_method: str) -> AiCallResult:
        return call_gemini(document_text, self._settings, source_method=source_method, client=self._client)
