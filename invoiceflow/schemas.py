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
from datetime import date, datetime, timezone
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
    def _blank_to_none(cls, value: Any, info) -> Any:  # noqa: ANN001
        if cls.model_fields[info.field_name].annotation in (str, str | None):
            return _clean_text(value)
        return value


class LineItemExtraction(_Lenient):
    description: str | None = Field(default=None, description="Item description exactly as printed.")
    item_code: str | None = Field(default=None, description="SKU / item / part code if printed, else null.")
    quantity: str | None = Field(default=None, description="Quantity as a plain decimal string, e.g. '12' or '2.5'.")
    unit_price: str | None = Field(default=None, description="Unit price as a plain decimal string, no currency symbol or thousands separator.")
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
    purchase_order_number: str | None = Field(default=None, description="The customer's PO number if one is printed on the invoice. Never guess or infer one.")

    # Vendor (the party ISSUING the invoice, not the customer / bill-to)
    vendor_name: str | None = Field(default=None, description="Name of the company issuing the invoice (the seller), not the bill-to customer.")
    vendor_address: str | None = Field(default=None, description="Vendor's address, single line.")
    vendor_tax_id: str | None = Field(default=None, description="Vendor's tax / VAT / EIN identifier as printed.")
    vendor_email: str | None = Field(default=None, description="Vendor's email address if printed.")

    # Financial (decimal strings, '.' as decimal separator, no symbols or thousands separators)
    currency: str | None = Field(default=None, description="ISO 4217 code (e.g. USD) if stated or unambiguous, else null.")
    subtotal: str | None = Field(default=None, description="Sum of line amounts BEFORE tax, shipping and discount, as printed.")
    tax_total: str | None = Field(default=None, description="Total of all taxes combined (add separate tax lines together only if no combined total is printed).")
    shipping_fee: str | None = Field(default=None, description="Shipping / freight / handling charge if printed.")
    discount: str | None = Field(default=None, description="Discount amount if printed, as a positive number.")
    grand_total: str | None = Field(default=None, description="Final amount due / invoice total, as printed.")

    line_items: list[LineItemExtraction] = Field(default_factory=list, description="EVERY line item, in document order, across all pages.")

    payment_terms: str | None = Field(default=None, description="Payment terms as printed, e.g. 'Net 30'.")
    extraction_notes: list[str] = Field(default_factory=list, description="Short notes about anything unreadable, ambiguous, inconsistent or uncertain. Empty if none.")
    missing_fields: list[str] = Field(default_factory=list, description="Names of the fields above that are not present in the document.")
    evidence: list[EvidenceSnippet] = Field(default_factory=list, description="Verbatim supporting snippets for the important fields (invoice_number, invoice_date, purchase_order_number, vendor_name, currency, subtotal, tax_total, grand_total).")


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
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
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
