"""Logging setup. Never log secrets; use ``Settings.safe_summary()`` for config."""

from __future__ import annotations

import logging
import os

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    """Configure root logging once (idempotent; Streamlit reruns call this every time)."""
    name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(getattr(logging, name, logging.INFO))
        return
    logging.basicConfig(level=getattr(logging, name, logging.INFO), format=LOG_FORMAT)
