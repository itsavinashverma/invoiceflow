"""Pydantic v2 schemas.

Two layers, deliberately separate:

* ``InvoiceExtraction`` is the *model-facing* shape: everything optional, money and quantities as
  plain strings exactly as the model reports them. This is what Gemini is asked to produce.
* ``NormalizedInvoice`` is what the rest of the system uses: ``Decimal`` money, ``date`` dates.
  It is produced only by ``services.normalizer`` (never by float arithmetic).

PO / decision schemas arrive with the data-model stage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ----------------------------------------------------------------------------- ingestion

PageMethod = Literal["text", "ocr", "none"]
ExtractionMethod = Literal["text", "ocr", "mixed"]


class PageResult(BaseModel):
    """What happened to one page."""

    page_number: int = Field(ge=1)  # 1-based
    method: PageMethod  # which source the final text came from ("none" = nothing usable)
    text: str
    meaningful_chars: int = Field(ge=0)  # alphanumeric characters in the final text
    text_layer_chars: int = Field(ge=0)  # alphanumeric characters found by normal extraction
    has_images: bool
    ocr_attempted: bool = False
    ocr_confidence: float | None = None  # mean word confidence 0-100, OCR pages only
    warnings: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """Structured outcome of ingesting one PDF."""

    text: str  # all pages, each prefixed with a "--- Page N of M ---" marker
    page_count: int = Field(ge=1)
    method: ExtractionMethod
    pages: list[PageResult]
    ocr_pages: list[int] = Field(default_factory=list)  # 1-based pages that required OCR
    warnings: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)
    file_sha256: str
    file_size_bytes: int = Field(ge=0)


# ----------------------------------------------------------------------------- AI extraction (model-facing)


def _clean_text(value: Any) -> Any:
    """Blank strings become None; ints become strings; floats are rejected (no binary floats)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected a string, got a boolean")
    if isinstance(value, float):
        raise ValueError("expected a decimal string, got a binary floating-point number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class _Lenient(BaseModel):
    """Base: tolerant of blanks, ignores unknown keys (the model may add some)."""

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any, info) -> Any:
        if cls.model_fields[info.field_name].annotation in (str, str | None):
            return _clean_text(value)
        return value


class LineItemExtraction(_Lenient):
    description: str | None = Field(default=None, description="Item description exactly as printed.")
    item_code: str | None = Field(default=None, description="SKU / item / part code if printed, else null.")
    quantity: str | None = Field(default=None, description="Quantity as a plain decimal string, e.g. '12' or '2.5'.")
    unit_price: str | None = Field(
        default=None, description="Unit price as a plain decimal string, no currency symbol or thousands separator."
    )
    line_total: str | None = Field(default=None, description="Line amount as printed, as a plain decimal string.")


class EvidenceSnippet(_Lenient):
    field: str = Field(description="Name of the extracted field this snippet supports, e.g. 'grand_total'.")
    snippet: str = Field(description="Short text (max ~120 chars) copied verbatim from the document.")


class InvoiceExtraction(_Lenient):
    """Fields extracted from one invoice. Every value is optional: null means 'not in the document'."""

    # Invoice information
    invoice_number: str | None = Field(default=None, description="Invoice number exactly as printed (keep prefixes, dashes, case).")
    invoice_date: str | None = Field(default=None, description="Invoice issue date in ISO format YYYY-MM-DD.")
    due_date: str | None = Field(default=None, description="Payment due date in ISO format YYYY-MM-DD, only if printed.")
    purchase_order_number: str | None = Field(
        default=None, description="The customer's PO number if one is printed on the invoice. Never guess or infer one."
    )

    # Vendor (the party ISSUING the invoice, not the customer / bill-to)
    vendor_name: str | None = Field(
        default=None, description="Name of the company issuing the invoice (the seller), not the bill-to customer."
    )
    vendor_address: str | None = Field(default=None, description="Vendor's address, single line.")
    vendor_tax_id: str | None = Field(default=None, description="Vendor's tax / VAT / EIN identifier as printed.")
    vendor_email: str | None = Field(default=None, description="Vendor's email address if printed.")

    # Financial (decimal strings, '.' as decimal separator, no symbols or thousands separators)
    currency: str | None = Field(default=None, description="ISO 4217 code (e.g. USD) if stated or unambiguous, else null.")
    subtotal: str | None = Field(default=None, description="Sum of line amounts BEFORE tax, shipping and discount, as printed.")
    tax_total: str | None = Field(
        default=None, description="Total of all taxes combined (add separate tax lines together only if no combined total is printed)."
    )
    shipping_fee: str | None = Field(default=None, description="Shipping / freight / handling charge if printed.")
    discount: str | None = Field(default=None, description="Discount amount if printed, as a positive number.")
    grand_total: str | None = Field(default=None, description="Final amount due / invoice total, as printed.")

    line_items: list[LineItemExtraction] = Field(default_factory=list, description="EVERY line item, in document order, across all pages.")

    payment_terms: str | None = Field(default=None, description="Payment terms as printed, e.g. 'Net 30'.")
    extraction_notes: list[str] = Field(
        default_factory=list, description="Short notes about anything unreadable, ambiguous, inconsistent or uncertain. Empty if none."
    )
    missing_fields: list[str] = Field(default_factory=list, description="Names of the fields above that are not present in the document.")
    evidence: list[EvidenceSnippet] = Field(
        default_factory=list,
        description="Verbatim supporting snippets for the important fields (invoice_number, invoice_date, purchase_order_number, vendor_name, currency, subtotal, tax_total, grand_total).",
    )


# ----------------------------------------------------------------------------- normalised (Decimal) layer


class NormalizedLineItem(BaseModel):
    description: str | None = None
    item_code: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None


class NormalizedInvoice(BaseModel):
    """Validated, typed invoice data. Money is Decimal, quantized only where stated in the normalizer."""

    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    purchase_order_number: str | None = None
    vendor_name: str | None = None
    vendor_address: str | None = None
    vendor_tax_id: str | None = None
    vendor_email: str | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    shipping_fee: Decimal | None = None
    discount: Decimal | None = None
    grand_total: Decimal | None = None
    line_items: list[NormalizedLineItem] = Field(default_factory=list)
    payment_terms: str | None = None
    missing_fields: list[str] = Field(default_factory=list)  # computed: fields that are actually null
    notes: list[str] = Field(default_factory=list)  # the model's extraction_notes, verbatim


# ----------------------------------------------------------------------------- purchase orders / stats


class PurchaseOrderData(BaseModel):
    """Validated purchase-order input (e.g. from the seed file). Money is Decimal, parsed from strings."""

    po_number: str = Field(min_length=1, max_length=64)
    vendor_name: str = Field(min_length=1, max_length=255)
    currency: str
    total_amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    tolerance_percent: Decimal = Field(default=Decimal("0.00"), ge=0, le=100, max_digits=5, decimal_places=2)
    po_date: date
    status: Literal["OPEN", "CLOSED"] = "OPEN"

    @field_validator("po_number", "vendor_name", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any) -> Any:
        v = v.strip().upper() if isinstance(v, str) else v
        if not isinstance(v, str) or len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return v

    @field_validator("total_amount", "tolerance_percent", mode="before")
    @classmethod
    def _no_floats(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("pass money as a string or Decimal, never a float")
        return v


class DashboardStats(BaseModel):
    """Aggregates for the dashboard. All money is Decimal, summed in Python (never SQL float math)."""

    total_runs: int = 0
    runs_by_status: dict[str, int] = Field(default_factory=dict)
    total_invoices: int = 0
    invoices_by_status: dict[str, int] = Field(default_factory=dict)
    invoiced_total_by_currency: dict[str, Decimal] = Field(default_factory=dict)  # every stored invoice
    approved_total_by_currency: dict[str, Decimal] = Field(default_factory=dict)  # APPROVED_STATUSES only
    open_purchase_orders: int = 0
    closed_purchase_orders: int = 0
    avg_run_duration_seconds: float | None = None
    runs_last_7_days: int = 0


# ----------------------------------------------------------------------------- decision engine (pure data)

DecisionStatus = Literal["APPROVED", "REVIEW_REQUIRED", "DUPLICATE_BLOCKED", "PROCESSING_FAILED"]


class DecisionConfig(BaseModel):
    """Tunable rules. Defaults are the documented policy."""

    amount_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)  # absolute rounding tolerance for invoice arithmetic
    po_amount_basis: Literal["pre_tax", "grand_total"] = "pre_tax"  # which invoice amount is compared with the PO


class PoSnapshot(BaseModel):
    """A purchase order as the engine sees it (no database objects leak into the rules)."""

    po_number: str
    vendor_name: str
    currency: str
    total_amount: Decimal
    tolerance_percent: Decimal
    status: Literal["OPEN", "CLOSED"]
    po_date: date | None = None


class PriorInvoiceRef(BaseModel):
    """A previously stored invoice, used for duplicate detection and PO balances."""

    run_id: str | None = None
    invoice_number: str | None = None
    vendor_name: str | None = None
    currency: str | None = None
    grand_total: Decimal | None = None
    tax_total: Decimal | None = None
    final_status: str
    file_hash: str | None = None


class PoSuggestion(BaseModel):
    """A PO that *might* belong to an invoice without a PO reference. Never applied automatically."""

    po_number: str
    vendor_name: str
    status: Literal["OPEN", "CLOSED"]
    currency: str
    total_amount: Decimal
    remaining_balance: Decimal | None = None


class DecisionInput(BaseModel):
    """Everything the engine needs. Built by ``services.decision_service`` from the database."""

    invoice: NormalizedInvoice
    file_hash: str | None = None
    po: PoSnapshot | None = None  # the PO found by exact (normalised) PO-number lookup
    po_number_matches: list[PoSuggestion] = Field(default_factory=list)  # every PO the normalised number matched (>1 = ambiguous)
    po_candidates: list[PoSuggestion] = Field(default_factory=list)  # vendor-based candidates, used only when no PO matched
    approved_invoices: list[PriorInvoiceRef] = Field(default_factory=list)  # already APPROVED against ``po``
    same_number_invoices: list[PriorInvoiceRef] = Field(default_factory=list)  # stored invoices: same vendor + invoice number
    same_file_invoices: list[PriorInvoiceRef] = Field(default_factory=list)  # stored invoices from a byte-identical file
    uncertainty_flags: list[str] = Field(default_factory=list)  # reasons the extraction cannot be trusted (OCR, invented values...)
    processing_error: str | None = None  # set when the document could not be processed at all


class CheckResult(BaseModel):
    check: str  # stable identifier, e.g. "po_is_open"
    title: str  # plain-English name of the check
    passed: bool
    detail: str  # what was found, in a sentence a finance reader can follow
    reason_code: str | None = None  # set for failed checks


class Reason(BaseModel):
    code: str
    message: str


class DecisionResult(BaseModel):
    """The final, explainable outcome. Contains no LLM output and triggers no payment."""

    status: DecisionStatus
    summary: str  # one paragraph in plain English
    invoice_number: str | None = None
    vendor_name: str | None = None
    matched_po_number: str | None = None
    currency: str | None = None
    invoice_total: Decimal | None = None  # grand total payable
    invoice_amount: Decimal | None = None  # the amount compared with the PO (see DecisionConfig.po_amount_basis)
    po_amount: Decimal | None = None
    already_approved_amount: Decimal | None = None
    remaining_balance: Decimal | None = None  # po_amount - already_approved_amount
    tolerance_percent: Decimal | None = None
    allowed_overage: Decimal | None = None  # po_amount * tolerance / 100, rounded down to the cent
    maximum_allowed_total: Decimal | None = None  # remaining_balance + allowed_overage
    reasons: list[Reason] = Field(default_factory=list)
    checks_passed: list[CheckResult] = Field(default_factory=list)
    checks_failed: list[CheckResult] = Field(default_factory=list)
    checks_skipped: list[CheckResult] = Field(default_factory=list)  # could not be run (e.g. no PO to compare with)
    human_actions: list[str] = Field(default_factory=list)
    po_suggestions: list[PoSuggestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # informational, not reasons
    basis: dict[str, str] = Field(default_factory=dict)  # the rules applied, for the audit trail

    @property
    def reason_codes(self) -> list[str]:
        return [r.code for r in self.reasons]


# ----------------------------------------------------------------------------- pipeline result

StageStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED"]


class StageEvent(BaseModel):
    """One pipeline stage as it actually happened. ``COMPLETED`` is only ever set after the stage succeeded."""

    stage_name: str
    title: str
    status: StageStatus = "PENDING"
    message: str | None = None  # short explanation of the outcome
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None


class OcrInfo(BaseModel):
    needed: bool  # did any page lack a usable text layer?
    pages: list[int] = Field(default_factory=list)  # 1-based pages that were (or needed to be) OCR'd
    performed: bool = False
    confidences: dict[int, float | None] = Field(default_factory=dict)  # mean Tesseract word confidence per OCR'd page
    text_method: str | None = None  # overall: "text", "ocr" or "mixed"
    warnings: list[str] = Field(default_factory=list)


class AiInfo(BaseModel):
    provider: str | None = None  # "gemini", or a clearly-labelled test double in automated tests
    is_test_double: bool = False
    used: bool = False  # False when the stage never ran or manual entry replaced it
    source: Literal["ai", "manual"] | None = None
    status: str | None = None  # success / unavailable / failed / manual
    model: str | None = None
    attempts: int = 0
    record_id: str | None = None  # id of the stored audit record (extraction_records)
    error_code: str | None = None
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    evidence: list[VerifiedEvidence] = Field(default_factory=list)


class PoMatchingInfo(BaseModel):
    matched_po_number: str | None = None
    po_amount: Decimal | None = None
    already_approved_amount: Decimal | None = None
    remaining_balance: Decimal | None = None
    suggestions: list[PoSuggestion] = Field(default_factory=list)  # never applied automatically
    checks: list[CheckResult] = Field(default_factory=list)


class PipelineError(BaseModel):
    """What went wrong and what the user can do about it."""

    stage: str
    code: str
    message: str
    action: str  # plain-English next step
    manual_entry_possible: bool = False  # True when entering the fields by hand is a sensible way forward


class PipelineResult(BaseModel):
    """Everything the pipeline learned about one upload. Safe to show; contains no secrets."""

    run_id: str
    run_status: Literal["RUNNING", "COMPLETED", "FAILED"]
    filename: str
    file_hash: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    invoice: NormalizedInvoice | None = None  # extracted and normalised values, if extraction got that far
    ingestion: ExtractionResult | None = None  # text, per-page details (partial if OCR was impossible)
    ocr: OcrInfo | None = None
    ai: AiInfo = Field(default_factory=AiInfo)
    po_matching: PoMatchingInfo | None = None
    validation: list[CheckResult] = Field(default_factory=list)  # duplicate, arithmetic and amount checks
    decision: DecisionResult | None = None
    decision_status: str | None = None  # APPROVED / REVIEW_REQUIRED / DUPLICATE_BLOCKED / PROCESSING_FAILED
    decision_reasons: list[Reason] = Field(default_factory=list)
    stages: list[StageEvent] = Field(default_factory=list)
    error: PipelineError | None = None
    persistence_error: str | None = None  # set only if even the failure record could not be saved


# ----------------------------------------------------------------------------- audit record

RecordSource = Literal["ai", "manual"]
RecordStatus = Literal["success", "unavailable", "failed", "manual"]


class VerifiedEvidence(BaseModel):
    field: str
    snippet: str
    found_in_source: bool  # did we find this snippet in the document text? (checked deterministically)


class AiExtractionRecord(BaseModel):
    """Everything needed to audit one extraction attempt, whether it succeeded, failed or was manual.

    Model-reported confidence is intentionally absent: it is not evidence of correctness.
    """

    record_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: RecordSource
    status: RecordStatus
    model: str | None = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attempts: int = 0
    file_sha256: str | None = None
    extraction: InvoiceExtraction | None = None  # validated model output / manual input, as strings
    normalized: NormalizedInvoice | None = None  # Decimal / date view of the above
    evidence: list[VerifiedEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_response: str | None = None  # the model's JSON text (never headers or keys); truncated
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ai_succeeded(self) -> bool:
        return self.source == "ai" and self.status == "success"
