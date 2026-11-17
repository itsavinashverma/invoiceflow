"""Document ingestion: validate, extract the text layer with PyMuPDF, OCR pages that need it."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pymupdf

from invoiceflow import constants
from invoiceflow.config import Settings, load_settings
from invoiceflow.exceptions import ExtractionError, NoTextExtractedError, OcrUnavailableError
from invoiceflow.extraction.ocr_extractor import ensure_tesseract, ocr_page
from invoiceflow.schemas import ExtractionMethod, ExtractionResult, PageResult
from invoiceflow.utils.pdf_security import PdfInfo, validate_pdf_upload

logger = logging.getLogger(__name__)

_METADATA_KEYS = ("title", "author", "subject", "creator", "producer", "creationDate", "modDate", "format")


def meaningful_chars(text: str) -> int:
    """Count alphanumeric characters: whitespace and punctuation noise do not count as content."""
    return sum(1 for c in text if c.isalnum())


def _extract_page(doc: pymupdf.Document, index: int) -> PageResult:
    """Text-layer extraction for one page. Never raises: failures are recorded on the page."""
    number = index + 1
    warnings: list[str] = []
    text = ""
    has_images = True  # assume the worst if the page cannot be inspected, so OCR gets a chance
    try:
        page = doc.load_page(index)
        text = page.get_text("text", sort=True).strip()
        has_images = bool(page.get_images(full=True))
    except Exception as exc:
        logger.warning("Page %d could not be read: %s", number, exc)
        warnings.append(f"Page {number} could not be read normally ({type(exc).__name__}).")
    chars = meaningful_chars(text)
    # Short text is accepted as real content only when there is no image that might hold the rest.
    usable = chars >= constants.MIN_MEANINGFUL_CHARS_PER_PAGE or (chars > 0 and not has_images)
    return PageResult(
        page_number=number,
        method="text" if usable else "none",
        text=text,
        meaningful_chars=chars,
        text_layer_chars=chars,
        has_images=has_images,
        warnings=warnings,
    )


def _needs_ocr(page: PageResult) -> bool:
    """Little text *and* an image to read. A page with neither is just blank."""
    return page.method == "none" and page.has_images


def _overall_method(pages: list[PageResult]) -> ExtractionMethod:
    used = {p.method for p in pages if p.method != "none"}
    if used == {"text"}:
        return "text"
    if used == {"ocr"}:
        return "ocr"
    return "mixed"


def _assemble(
    pages: list[PageResult], *, info_warnings: list[str], metadata: dict[str, str], sha256: str, size: int, started: float
) -> ExtractionResult:
    total = len(pages)
    warnings = list(info_warnings)
    for p in pages:
        warnings.extend(p.warnings)
    chunks = [f"--- Page {p.page_number} of {total} ---\n{p.text}" for p in pages]
    used = [p for p in pages if p.method != "none"]
    return ExtractionResult(
        text="\n\n".join(chunks),
        page_count=total,
        method=_overall_method(pages) if used else "text",
        pages=pages,
        ocr_pages=[p.page_number for p in pages if p.method == "ocr"],
        warnings=warnings,
        duration_seconds=round(time.perf_counter() - started, 3),
        metadata=metadata,
        file_sha256=sha256,
        file_size_bytes=size,
    )


@dataclass
class TextLayer:
    """State between the stages: validated upload plus per-page results from the text layer."""

    data: bytes
    info: PdfInfo
    pages: list[PageResult]
    metadata: dict[str, str]
    started: float

    def assemble(self) -> ExtractionResult:
        return _assemble(
            self.pages,
            info_warnings=self.info.warnings,
            metadata=self.metadata,
            sha256=self.info.sha256,
            size=self.info.size_bytes,
            started=self.started,
        )


def read_text_layer(data: bytes, info: PdfInfo) -> TextLayer:
    """Stage: read the embedded text of every page with PyMuPDF (no OCR). ``info`` comes from validation."""
    started = time.perf_counter()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ExtractionError("The PDF could not be opened for extraction.") from exc
    with doc:
        metadata = {k: v for k in _METADATA_KEYS if (v := (doc.metadata or {}).get(k))}
        pages = [_extract_page(doc, i) for i in range(doc.page_count)]
    return TextLayer(data=data, info=info, pages=pages, metadata=metadata, started=started)


def pages_needing_ocr(layer: TextLayer) -> list[int]:
    """1-based numbers of pages with too little text and an image that may hold the rest."""
    return [p.page_number for p in layer.pages if _needs_ocr(p)]


def run_ocr(layer: TextLayer, settings: Settings) -> None:
    """Stage: OCR the pages that need it, updating ``layer`` in place.

    Raises :class:`OcrUnavailableError` (with ``partial_result``) if Tesseract cannot be used, or
    :class:`ExtractionError` if OCR fails on a page. Does nothing when no page needs OCR.
    """
    numbers = pages_needing_ocr(layer)
    if not numbers:
        return
    logger.info("Pages needing OCR: %s", numbers)
    try:
        ensure_tesseract(settings.tesseract_cmd)
    except OcrUnavailableError as exc:
        raise OcrUnavailableError(
            f"Page(s) {', '.join(map(str, numbers))} have no readable text layer and need OCR. {exc}",
            partial_result=layer.assemble(),
        ) from exc
    try:
        doc = pymupdf.open(stream=layer.data, filetype="pdf")
    except Exception as exc:
        raise ExtractionError("The PDF could not be reopened for OCR.") from exc
    with doc:
        for p in layer.pages:
            if p.page_number not in numbers:
                continue
            out = ocr_page(doc.load_page(p.page_number - 1), p.page_number)
            p.ocr_attempted = True
            p.ocr_confidence = out.confidence
            ocr_chars = meaningful_chars(out.text)
            if ocr_chars > p.meaningful_chars:
                p.text, p.meaningful_chars, p.method = out.text.strip(), ocr_chars, "ocr"
            if p.method == "none" and p.meaningful_chars > 0:
                p.method = "text"  # OCR found nothing better; keep the sparse text layer
            if p.meaningful_chars < constants.MIN_MEANINGFUL_CHARS_PER_PAGE:
                p.warnings.append(f"Page {p.page_number}: OCR found almost no text.")
            elif out.confidence is not None and out.confidence < constants.OCR_LOW_CONFIDENCE:
                p.warnings.append(f"Page {p.page_number}: low OCR confidence ({out.confidence:.0f}/100); verify the data.")


def finalize(layer: TextLayer) -> ExtractionResult:
    """Stage end: flag blank pages and build the result. Raises :class:`NoTextExtractedError` if nothing was read."""
    for p in layer.pages:
        if p.method == "none" and not p.ocr_attempted and not p.warnings:
            p.warnings.append(f"Page {p.page_number} appears to be blank.")
    result = layer.assemble()
    if not any(p.method != "none" for p in layer.pages):
        raise NoTextExtractedError("No readable text was found in the PDF, neither in its text layer nor via OCR.")
    logger.info("Extracted %d page(s) via %s in %.2fs", result.page_count, result.method, result.duration_seconds)
    return result


def extract_document(
    data: bytes,
    filename: str | None = None,
    settings: Settings | None = None,
    *,
    content_type: str | None = None,
) -> ExtractionResult:
    """Validate and extract text from an uploaded PDF held in memory (all stages in one call).

    Raises:
        PdfValidationError: rejected upload (wrong type, too large, corrupt, encrypted, ...).
        OcrUnavailableError: pages need OCR but Tesseract is missing/unusable (``partial_result`` set).
        NoTextExtractedError: no page yielded meaningful text.
        ExtractionError: any other extraction failure (e.g. OCR error on a page).
    """
    settings = settings or load_settings()
    info = validate_pdf_upload(
        data,
        filename,
        max_bytes=settings.max_upload_bytes,
        max_pages=settings.max_pdf_pages,
        content_type=content_type,
    )
    layer = read_text_layer(data, info)
    run_ocr(layer, settings)
    return finalize(layer)
