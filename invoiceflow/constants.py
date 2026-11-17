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

# Database vocabulary (enforced by CHECK constraints)
PO_OPEN, PO_CLOSED = "OPEN", "CLOSED"
PO_STATUSES = (PO_OPEN, PO_CLOSED)

RUN_RUNNING, RUN_COMPLETED, RUN_FAILED = "RUNNING", "COMPLETED", "FAILED"
RUN_STATUSES = (RUN_RUNNING, RUN_COMPLETED, RUN_FAILED)

STAGE_PENDING, STAGE_RUNNING, STAGE_COMPLETED, STAGE_FAILED, STAGE_SKIPPED = (
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "SKIPPED",
)
STAGE_STATUSES = (STAGE_PENDING, STAGE_RUNNING, STAGE_COMPLETED, STAGE_FAILED, STAGE_SKIPPED)

# Pipeline stages in execution order, with the title shown to users.
STAGE_FILE_HASH = "file_hash"
STAGE_VALIDATION = "upload_validation"
STAGE_TEXT = "text_extraction"
STAGE_OCR = "ocr"
STAGE_AI = "ai_extraction"
STAGE_STRUCTURE = "structure_validation"
STAGE_NORMALIZE = "normalization"
STAGE_DUPLICATES = "duplicate_check"
STAGE_PO = "po_matching"
STAGE_FINANCIAL = "financial_validation"
STAGE_DECISION = "decision"
STAGE_SAVE = "save_result"
PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    (STAGE_FILE_HASH, "File fingerprint (SHA-256)"),
    (STAGE_VALIDATION, "Document validation"),
    (STAGE_TEXT, "PDF text extraction"),
    (STAGE_OCR, "OCR (only if needed)"),
    (STAGE_AI, "AI information extraction"),
    (STAGE_STRUCTURE, "Structured-output validation"),
    (STAGE_NORMALIZE, "Data normalization"),
    (STAGE_DUPLICATES, "Duplicate detection"),
    (STAGE_PO, "Purchase order matching"),
    (STAGE_FINANCIAL, "Financial validation"),
    (STAGE_DECISION, "Decision generation"),
    (STAGE_SAVE, "Database persistence"),
)

# Invoice outcomes. The first four are produced by the deterministic decision engine; REJECTED records
# a *human* decision made afterwards. APPROVED only means "cleared by the rules / a reviewer": nothing
# in this system ever triggers a payment or touches an accounting system.
DECISION_APPROVED = "APPROVED"
DECISION_REVIEW_REQUIRED = "REVIEW_REQUIRED"
DECISION_DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
DECISION_PROCESSING_FAILED = "PROCESSING_FAILED"
DECISION_REJECTED = "REJECTED"
INVOICE_STATUSES = (
    DECISION_APPROVED,
    DECISION_REVIEW_REQUIRED,
    DECISION_DUPLICATE_BLOCKED,
    DECISION_PROCESSING_FAILED,
    DECISION_REJECTED,
)
# Only these count against a PO's balance. Review-pending, duplicate, failed and rejected do not.
APPROVED_STATUSES = (DECISION_APPROVED,)

# Decision rules
DEFAULT_AMOUNT_TOLERANCE = "0.01"  # absolute rounding tolerance for invoice arithmetic, in currency units
PO_BASIS_PRE_TAX = "pre_tax"  # compare (grand total - tax) with the PO: POs are usually quoted net of tax
PO_BASIS_GRAND_TOTAL = "grand_total"  # compare the full amount payable with the PO
PO_AMOUNT_BASES = (PO_BASIS_PRE_TAX, PO_BASIS_GRAND_TOTAL)

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
