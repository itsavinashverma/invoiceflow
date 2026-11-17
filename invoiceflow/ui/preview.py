"""Page previews of an uploaded PDF (rendered images; nothing in the PDF is executed or fetched)."""

from __future__ import annotations

import logging

import pymupdf

logger = logging.getLogger(__name__)


def render_page_png(data: bytes, page_index: int = 0, *, width_px: int = 760) -> bytes | None:
    """PNG of one page scaled to ``width_px``, or ``None`` if it cannot be rendered. Never raises."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if not 0 <= page_index < doc.page_count:
                return None
            page = doc.load_page(page_index)
            zoom = max(0.2, min(width_px / max(page.rect.width, 1), 3.0))
            return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    except Exception:
        logger.info("Could not render a preview", exc_info=True)
        return None
