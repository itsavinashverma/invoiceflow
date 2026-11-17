"""Tesseract OCR fallback for pages without a usable text layer."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pymupdf
import pytesseract
from PIL import Image
from pytesseract import Output

from invoiceflow import constants
from invoiceflow.exceptions import ExtractionError, OcrUnavailableError

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "Install Tesseract OCR (Windows: https://github.com/UB-Mannheim/tesseract/wiki or "
    "`winget install UB-Mannheim.TesseractOCR`; Linux/Streamlit Cloud: add `tesseract-ocr` to packages.txt) "
    "or set TESSERACT_CMD to the full path of tesseract.exe."
)


@dataclass(frozen=True)
class OcrOutput:
    text: str
    confidence: float | None  # mean word confidence, 0-100; None if no words were recognised


def ensure_tesseract(cmd: str | None, lang: str = constants.OCR_LANG) -> str:
    """Point pytesseract at ``cmd`` and verify it runs and has ``lang`` data. Returns the version.

    Raises :class:`OcrUnavailableError` with an actionable message otherwise.
    """
    pytesseract.pytesseract.tesseract_cmd = cmd or "tesseract"  # always reset: never leak a previous setting
    try:
        version = str(pytesseract.get_tesseract_version())
        languages = pytesseract.get_languages(config="")
    except pytesseract.TesseractNotFoundError as exc:
        where = f"at {cmd!r}" if cmd else "on PATH"
        raise OcrUnavailableError(f"Tesseract OCR was not found {where}. {INSTALL_HINT}") from exc
    except Exception as exc:  # noqa: BLE001 - broken install, bad permissions, policy blocks, ...
        raise OcrUnavailableError(f"Tesseract OCR could not be run ({type(exc).__name__}: {exc}). {INSTALL_HINT}") from exc
    if lang not in languages:
        raise OcrUnavailableError(
            f"Tesseract is installed but the {lang!r} language data is missing (found: {', '.join(languages) or 'none'}). "
            "Install the English traineddata (Linux package `tesseract-ocr-eng`)."
        )
    return version


def render_page(page: pymupdf.Page, dpi: int = constants.OCR_DPI, max_side_px: int = constants.OCR_MAX_IMAGE_SIDE_PX) -> Image.Image:
    """Render a page to a grayscale Pillow image, shrinking the DPI if the page would be huge."""
    zoom = dpi / 72
    longest_pt = max(page.rect.width, page.rect.height)
    if longest_pt * zoom > max_side_px:
        zoom = max_side_px / longest_pt
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def ocr_image(image: Image.Image, lang: str = constants.OCR_LANG, timeout: int = constants.OCR_TIMEOUT_SECONDS) -> OcrOutput:
    """Run Tesseract on an image, rebuilding line-structured text and a mean confidence."""
    data = pytesseract.image_to_data(image, lang=lang, output_type=Output.DICT, timeout=timeout)
    lines: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        conf = float(data["conf"][i])
        if conf >= 0:
            confs.append(conf)
    out: list[str] = []
    prev_block: int | None = None
    for (block, _par, _line), words in lines.items():
        if prev_block is not None and block != prev_block:
            out.append("")  # blank line between layout blocks
        out.append(" ".join(words))
        prev_block = block
    return OcrOutput(text="\n".join(out), confidence=round(sum(confs) / len(confs), 1) if confs else None)


def ocr_page(page: pymupdf.Page, page_number: int, lang: str = constants.OCR_LANG) -> OcrOutput:
    """Render and OCR one page. Raises :class:`ExtractionError` on failure (never returns fake text)."""
    try:
        return ocr_image(render_page(page), lang=lang)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailableError(f"Tesseract OCR disappeared while processing page {page_number}. {INSTALL_HINT}") from exc
    except RuntimeError as exc:  # pytesseract signals timeouts as RuntimeError
        raise ExtractionError(f"OCR failed on page {page_number}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"OCR failed on page {page_number} ({type(exc).__name__}: {exc})") from exc
