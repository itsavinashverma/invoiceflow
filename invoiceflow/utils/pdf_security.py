"""Upload validation. Nothing here trusts the filename or the browser-supplied MIME type.

Uploads are validated and processed entirely in memory: no temporary file is ever written, so
there is nothing to clean up and nothing persisted. PyMuPDF does not execute PDF JavaScript or
fetch external resources; we additionally flag documents that contain such features.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePath

import pymupdf

from invoiceflow import constants
from invoiceflow.exceptions import PdfValidationError

logger = logging.getLogger(__name__)

# Best-effort scan of raw bytes. Objects inside compressed streams are not visible to it,
# so this is a warning signal, never a security guarantee.
_ACTIVE_CONTENT = {
    rb"/JavaScript": "JavaScript",
    rb"/JS\b": "JavaScript",
    rb"/Launch": "launch actions",
    rb"/EmbeddedFile": "embedded files",
    rb"/OpenAction": "automatic open actions",
}


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    size_bytes: int
    sha256: str
    warnings: list[str] = field(default_factory=list)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def validate_pdf_upload(
    data: bytes,
    filename: str | None = None,
    *,
    max_bytes: int = constants.DEFAULT_MAX_UPLOAD_MB * 1024 * 1024,
    max_pages: int = constants.DEFAULT_MAX_PDF_PAGES,
    content_type: str | None = None,
) -> PdfInfo:
    """Validate an uploaded file. Raises :class:`PdfValidationError` with a user-safe message."""
    if filename is not None:
        name = PurePath(filename.replace("\\", "/")).name
        if not name.lower().endswith(".pdf"):
            raise PdfValidationError("Only PDF files are accepted (the file name must end in .pdf).", "bad_extension")
    if content_type is not None and content_type.lower().strip() not in constants.ALLOWED_PDF_CONTENT_TYPES:
        raise PdfValidationError(f"Unsupported content type {content_type!r}; upload a PDF.", "bad_content_type")

    if not data:
        raise PdfValidationError("The file is empty.", "empty_file")
    if len(data) > max_bytes:
        raise PdfValidationError(
            f"The file is {_mb(len(data))}, above the {_mb(max_bytes)} limit.", "file_too_large"
        )
    if constants.PDF_MAGIC not in data[: constants.PDF_MAGIC_SEARCH_BYTES]:
        raise PdfValidationError(
            "The file is not a real PDF (missing PDF signature), whatever its name says.", "bad_signature"
        )

    warnings: list[str] = []
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - PyMuPDF raises several types for bad input
        logger.info("PDF failed to open: %s", exc)
        raise PdfValidationError("The PDF is corrupt or unreadable and could not be opened.", "corrupt_pdf") from exc

    with doc:
        if doc.needs_pass:
            raise PdfValidationError("The PDF is password-protected. Remove the password and upload again.", "encrypted")
        if doc.is_encrypted:
            warnings.append("The PDF is encrypted (no password needed to open).")
        page_count = doc.page_count
        if page_count < 1:
            raise PdfValidationError("The PDF contains no pages.", "no_pages")
        if page_count > max_pages:
            raise PdfValidationError(
                f"The PDF has {page_count} pages, above the {max_pages}-page limit.", "too_many_pages"
            )
        try:
            doc.load_page(0)
        except Exception as exc:  # noqa: BLE001
            raise PdfValidationError("The PDF is corrupt: its first page cannot be read.", "corrupt_pdf") from exc
        if doc.is_repaired:
            warnings.append("The PDF structure was damaged and was repaired while opening; verify the extracted data.")

    found = sorted({label for pattern, label in _ACTIVE_CONTENT.items() if re.search(pattern, data)})
    if found:
        warnings.append(f"The PDF contains {', '.join(found)}; these are ignored and never executed.")

    return PdfInfo(
        page_count=page_count,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        warnings=warnings,
    )
