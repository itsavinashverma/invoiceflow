"""Central configuration.

Lookup order for every setting (first hit wins):
    1. real process environment variables
    2. Streamlit secrets (``.streamlit/secrets.toml`` locally, the Secrets panel on Streamlit Cloud)
    3. a local ``.env`` file
    4. built-in default

A missing ``GEMINI_API_KEY`` is *not* an error here; callers decide how to degrade visibly.
Secrets are never logged: use :meth:`Settings.safe_summary` for anything user-facing.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import dotenv_values

from invoiceflow import constants

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "invoiceflow.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"


class ConfigError(ValueError):
    """Raised when a configuration value is present but invalid."""


def normalize_database_url(url: str) -> str:
    """Force PostgreSQL URLs onto the psycopg (v3) driver.

    Accepts ``postgres://``, ``postgresql://`` and ``postgresql+psycopg://`` (as handed out by
    Supabase / Heroku-style providers) and returns ``postgresql+psycopg://``. Other URLs
    (e.g. ``sqlite:///...``) pass through unchanged.
    """
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + url[len("postgresql+psycopg2://") :]
    return url


def _streamlit_secrets() -> Mapping[str, object]:
    """Return Streamlit secrets as a flat mapping, or ``{}`` when unavailable.

    Never raises: running outside Streamlit, or without a secrets file, is normal.
    """
    try:
        import streamlit as st

        return {str(k): v for k, v in st.secrets.items() if not isinstance(v, Mapping)}
    except Exception:
        return {}


def _int_setting(name: str, raw: str | None, default: int, minimum: int = 1) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _decimal_setting(name: str, raw: str | None, default: str) -> Decimal:
    text = raw if raw not in (None, "") else default
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number, got {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name} must be zero or positive, got {text!r}")
    return value


def _choice_setting(name: str, raw: str | None, choices: tuple[str, ...], default: str) -> str:
    value = (raw or default).strip().lower()
    if value not in choices:
        raise ConfigError(f"{name} must be one of {', '.join(choices)}, got {raw!r}")
    return value


def _resolve_tesseract(explicit: str | None) -> str | None:
    """Explicit setting > PATH > default Windows install location > None (OCR unavailable)."""
    if explicit:
        return explicit
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    if os.name == "nt" and Path(constants.WINDOWS_TESSERACT_PATH).exists():
        return constants.WINDOWS_TESSERACT_PATH
    return None


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    database_url: str
    max_upload_mb: int
    max_pdf_pages: int
    tesseract_cmd: str | None
    gemini_timeout_seconds: int = constants.DEFAULT_GEMINI_TIMEOUT_SECONDS
    amount_tolerance: Decimal = Decimal(constants.DEFAULT_AMOUNT_TOLERANCE)  # absolute rounding tolerance for invoice arithmetic
    po_amount_basis: str = constants.PO_BASIS_PRE_TAX  # "pre_tax" or "grand_total"
    access_code: str | None = None  # optional shared code that visitors must enter (a secret: never displayed)

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_access_gate(self) -> bool:
        return bool(self.access_code)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def safe_summary(self) -> dict[str, str]:
        """Description safe to log or display: no API key, no database credentials."""
        db = self.database_url
        if "@" in db:  # drop "scheme://user:password@"
            scheme = db.split("://", 1)[0]
            db = f"{scheme}://***@{db.rsplit('@', 1)[-1]}"
        return {
            "gemini_model": self.gemini_model,
            "gemini_api_key": "set" if self.has_gemini_key else "NOT SET",
            "database": db,
            "max_upload_mb": str(self.max_upload_mb),
            "max_pdf_pages": str(self.max_pdf_pages),
            "tesseract_cmd": self.tesseract_cmd or "not found (OCR unavailable)",
            "access_gate": "enabled" if self.has_access_gate else "disabled",
        }


def load_settings(env_file: Path | None = None) -> Settings:
    """Build :class:`Settings` from env, Streamlit secrets and ``.env`` (see module docstring)."""
    dotenv = {k: v for k, v in dotenv_values(env_file or PROJECT_ROOT / ".env").items() if v is not None}
    secrets = _streamlit_secrets()

    def get(name: str) -> str | None:
        for source in (os.environ, secrets, dotenv):
            value = source.get(name)
            if value is not None and str(value).strip() != "":
                return str(value).strip()
        return None

    return Settings(
        gemini_api_key=get("GEMINI_API_KEY"),
        gemini_model=get("GEMINI_MODEL") or constants.DEFAULT_GEMINI_MODEL,
        database_url=normalize_database_url(get("DATABASE_URL") or DEFAULT_DATABASE_URL),
        max_upload_mb=_int_setting("MAX_UPLOAD_MB", get("MAX_UPLOAD_MB"), constants.DEFAULT_MAX_UPLOAD_MB),
        max_pdf_pages=_int_setting("MAX_PDF_PAGES", get("MAX_PDF_PAGES"), constants.DEFAULT_MAX_PDF_PAGES),
        tesseract_cmd=_resolve_tesseract(get("TESSERACT_CMD")),
        gemini_timeout_seconds=_int_setting(
            "GEMINI_TIMEOUT_SECONDS", get("GEMINI_TIMEOUT_SECONDS"), constants.DEFAULT_GEMINI_TIMEOUT_SECONDS
        ),
        amount_tolerance=_decimal_setting("AMOUNT_TOLERANCE", get("AMOUNT_TOLERANCE"), constants.DEFAULT_AMOUNT_TOLERANCE),
        po_amount_basis=_choice_setting("PO_AMOUNT_BASIS", get("PO_AMOUNT_BASIS"), constants.PO_AMOUNT_BASES, constants.PO_BASIS_PRE_TAX),
        access_code=get("ACCESS_CODE"),
    )


def is_hosted_environment() -> bool:
    """True when running on a hosted platform (Streamlit Community Cloud mounts the repo under ``/mount/src``).

    ``INVOICEFLOW_HOSTED=1`` forces it, for other hosts.
    """
    if os.environ.get("INVOICEFLOW_HOSTED", "").strip().lower() in ("1", "true", "yes"):
        return True
    return PROJECT_ROOT.as_posix().startswith("/mount/src")


def storage_warning(settings: Settings) -> str | None:
    """A warning when data would silently be lost: SQLite on a hosted platform's ephemeral disk."""
    if settings.is_sqlite and is_hosted_environment():
        return (
            "This deployment stores data in a local SQLite file on temporary hosted storage. It will be erased whenever the app "
            "restarts or is redeployed. Set DATABASE_URL to a PostgreSQL database (for example Supabase) in the app's secrets."
        )
    return None
