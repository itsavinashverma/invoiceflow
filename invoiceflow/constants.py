"""Project-wide constants. No business rules live here yet."""
from __future__ import annotations

APP_NAME = "InvoiceFlow"
APP_TAGLINE = "AI-assisted invoice processing with deterministic, human-in-the-loop decisions"

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_MAX_UPLOAD_MB = 10
DEFAULT_MAX_PDF_PAGES = 10

WINDOWS_TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

PDF_MAGIC = b"%PDF-"
PDF_MAGIC_SEARCH_BYTES = 1024  # the PDF spec allows junk before the header within the first 1 KiB
ALLOWED_PDF_CONTENT_TYPES = frozenset({"", "application/pdf", "application/x-pdf", "application/octet-stream"})

# Gemini client
DEFAULT_GEMINI_TIMEOUT_SECONDS = 60
GEMINI_MAX_ATTEMPTS = 3  # total attempts including the first call (i.e. at most 2 retries)
GEMINI_BASE_DELAY_SECONDS = 1.0
GEMINI_MAX_DELAY_SECONDS = 20.0  # never sleep longer than this; fail visibly instead
GEMINI_MAX_OUTPUT_TOKENS = 16384
MAX_DOCUMENT_CHARS = 150_000  # longer documents are truncated, with a warning

# Extraction / OCR tuning
MIN_MEANINGFUL_CHARS_PER_PAGE = 40  # alphanumeric characters below which a page counts as "no text"
OCR_DPI = 300
OCR_MAX_IMAGE_SIDE_PX = 5000  # cap render size so a huge page cannot exhaust memory
OCR_LANG = "eng"
OCR_TIMEOUT_SECONDS = 60
OCR_LOW_CONFIDENCE = 60.0  # mean Tesseract word confidence (0-100) below which we warn
