"""Gemini client built on the official ``google-genai`` SDK.

* structured output: ``response_mime_type="application/json"`` + ``response_json_schema`` from a
  Pydantic model, then the reply is validated with that same model;
* a small, bounded retry budget (default 3 attempts in total) for transient errors only;
* every failure is mapped to a typed :class:`~invoiceflow.exceptions.AiError` with a message that
  never contains the API key.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from invoiceflow import constants
from invoiceflow.config import Settings
from invoiceflow.exceptions import (
    AiError,
    GeminiAuthError,
    GeminiBlockedError,
    GeminiConfigError,
    GeminiInvalidResponseError,
    GeminiRateLimitError,
    GeminiRequestError,
    GeminiUnavailableError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_BLOCKING_FINISH = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION", "IMAGE_SAFETY"}


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    parsed: T
    raw_text: str
    model: str
    attempts: int


def validate_api_key_format(api_key: str | None) -> str:
    """Cheap local sanity check (no network). Real validation happens on first use / ``check_connection``."""
    if not api_key or not api_key.strip():
        raise GeminiConfigError("GEMINI_API_KEY is not set. Add it to .env or Streamlit secrets (create a free key in Google AI Studio).")
    key = api_key.strip()
    if re.search(r"\s", key) or len(key) < 20:
        raise GeminiConfigError("GEMINI_API_KEY looks malformed (too short or contains whitespace). Re-copy it from Google AI Studio.")
    return key


def redact(text: str, api_key: str | None = None) -> str:
    """Remove anything key-shaped (and the configured key itself) from text before logging/showing it."""
    if api_key:
        text = text.replace(api_key, "***")
    return _KEY_PATTERN.sub("***", text)


def _retry_delay_from(exc: genai_errors.APIError) -> float | None:
    """Parse the server's suggested delay (``RetryInfo.retryDelay`` like '27s') if present."""
    details = getattr(exc, "details", None)
    blob = json.dumps(details, default=str) if details else ""
    match = re.search(r"retryDelay\W+(\d+(?:\.\d+)?)s", blob)
    return float(match.group(1)) if match else None


def classify_exception(exc: Exception, api_key: str | None = None) -> AiError:
    """Map any SDK / network exception to a typed, user-safe :class:`AiError`."""
    if isinstance(exc, AiError):
        return exc
    if isinstance(exc, genai_errors.APIError):
        code = int(getattr(exc, "code", 0) or 0)
        detail = redact(str(getattr(exc, "message", "") or exc), api_key)[:300]
        if code == 429:
            delay = _retry_delay_from(exc)
            hint = f" Suggested wait: ~{delay:.0f}s." if delay else ""
            return GeminiRateLimitError(
                f"Gemini rate limit or quota reached (free-tier limits apply).{hint}", retry_after=delay
            )
        if code in (401, 403) or "API key not valid" in detail or "API_KEY_INVALID" in detail:
            return GeminiAuthError(f"Gemini rejected the API key or this request is not permitted (HTTP {code}). Check GEMINI_API_KEY.")
        if code == 404:
            return GeminiConfigError("The configured Gemini model was not found. Check GEMINI_MODEL.")
        if code in (408, 500, 502, 503, 504):
            return GeminiUnavailableError(f"Gemini is temporarily unavailable (HTTP {code}).")
        if code >= 500:
            return GeminiUnavailableError(f"Gemini returned a server error (HTTP {code}).")
        return GeminiRequestError(f"Gemini rejected the request (HTTP {code}): {detail}")
    name = type(exc).__name__
    if any(tok in name for tok in ("Timeout", "Connect", "Network", "Read", "Remote", "Protocol")) or isinstance(exc, (TimeoutError, ConnectionError)):
        return GeminiUnavailableError(f"Could not reach Gemini ({name}): check the network connection or try again.")
    logger.exception("Unexpected error calling Gemini")
    return GeminiUnavailableError(f"Unexpected error while calling Gemini ({name}).")


def _candidate_text(response: types.GenerateContentResponse, api_key: str | None) -> str:
    """Return the JSON text of a response, or raise a typed error. Never returns fabricated text."""
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise GeminiBlockedError(f"Gemini blocked the request ({feedback.block_reason}).")
    candidates = response.candidates or []
    if not candidates:
        raise GeminiInvalidResponseError("Gemini returned no candidates.")
    cand = candidates[0]
    reason = getattr(cand.finish_reason, "name", str(cand.finish_reason or ""))
    if reason in _BLOCKING_FINISH:
        raise GeminiBlockedError(f"Gemini stopped without an answer ({reason}).")
    parts = getattr(cand.content, "parts", None) or []
    text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
    if reason == "MAX_TOKENS":
        err = GeminiInvalidResponseError("Gemini's answer was cut off (output token limit); the JSON is incomplete.", raw_response=text)
        err.retryable = False  # same input would be cut off again
        raise err
    if not text.strip():
        raise GeminiInvalidResponseError(f"Gemini returned an empty answer (finish reason: {reason or 'unknown'}).")
    return text


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = constants.DEFAULT_GEMINI_TIMEOUT_SECONDS,
        max_attempts: int = constants.GEMINI_MAX_ATTEMPTS,
        base_delay: float = constants.GEMINI_BASE_DELAY_SECONDS,
        max_delay: float = constants.GEMINI_MAX_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        client: Any | None = None,
    ) -> None:
        self._api_key = validate_api_key_format(api_key)
        self.model = model
        self.max_attempts = max(1, max_attempts)
        self._base_delay, self._max_delay, self._sleep = base_delay, max_delay, sleep
        self._timeout_seconds = timeout_seconds
        # SDK-level retries are disabled (attempts=1) so the budget above is the only one.
        self._client = client or genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "GeminiClient":
        return cls(
            settings.gemini_api_key or "",
            settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            **kwargs,
        )

    # ------------------------------------------------------------------ public API

    def check_connection(self) -> str:
        """Verify the key and model with one cheap metadata call. Returns the model's resource name."""
        try:
            return str(self._client.models.get(model=self.model).name)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc, self._api_key) from None

    def generate_structured(
        self, *, system_instruction: str, contents: str, response_model: type[T]
    ) -> StructuredResult[T]:
        """Call Gemini and return a validated ``response_model``.

        Retries only transient failures (rate limit, 5xx/timeout, malformed output) within
        ``max_attempts`` total calls; auth, config, blocked and bad-request errors fail at once.
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.0,
            max_output_tokens=constants.GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_json_schema=response_model.model_json_schema(),
        )
        last: AiError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.models.generate_content(model=self.model, contents=contents, config=config)
                raw = _candidate_text(response, self._api_key)
                parsed = self._parse(raw, response_model)
                return StructuredResult(parsed=parsed, raw_text=raw, model=self.model, attempts=attempt)
            except Exception as exc:  # noqa: BLE001 - classified below
                err = classify_exception(exc, self._api_key)
                last = err
                logger.warning("Gemini attempt %d/%d failed: %s (%s)", attempt, self.max_attempts, err.code, err)
                if not err.retryable or attempt == self.max_attempts:
                    break
                delay = self._delay_for(err, attempt)
                if delay is None:
                    break  # server asked us to wait longer than we are willing to
                self._sleep(delay)
        assert last is not None
        if last.retryable and attempt > 1:
            last = type(last)(f"{last} (gave up after {attempt} attempts)", raw_response=last.raw_response, retry_after=last.retry_after)
        last.attempts = attempt
        raise last

    # ------------------------------------------------------------------ helpers

    def _delay_for(self, err: AiError, attempt: int) -> float | None:
        if err.retry_after is not None:
            return err.retry_after if err.retry_after <= self._max_delay else None
        delay = min(self._max_delay, self._base_delay * (2 ** (attempt - 1)))
        return delay + random.uniform(0, delay * 0.1)

    @staticmethod
    def _parse(raw: str, model: type[T]) -> T:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GeminiInvalidResponseError(f"Gemini's answer was not valid JSON ({exc.msg} at char {exc.pos}).", raw_response=raw) from None
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            # Report where it failed, not the offending values.
            where = ", ".join(sorted({".".join(map(str, e["loc"])) or "<root>" for e in exc.errors()}))[:200]
            raise GeminiInvalidResponseError(f"Gemini's JSON did not match the invoice schema (problem fields: {where}).", raw_response=raw) from None
