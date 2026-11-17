"""Deterministic normalisation of extracted values. No floats anywhere.

``normalize_extraction`` turns the model-facing ``InvoiceExtraction`` (strings) into a typed
``NormalizedInvoice`` (Decimal / date). Anything that cannot be parsed becomes ``None`` plus an
explicit warning: values are never guessed or silently repaired.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from invoiceflow.schemas import (
    EvidenceSnippet,
    InvoiceExtraction,
    NormalizedInvoice,
    NormalizedLineItem,
    VerifiedEvidence,
)

CENT = Decimal("0.01")
UNIT_PRICE_QUANTUM = Decimal("0.0001")
_CURRENCY_SYMBOLS = "$€£¥₹"
_TEXT_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%B-%Y")
_MONEY_FIELDS = ("subtotal", "tax_total", "shipping_fee", "discount", "grand_total")
# Fields whose absence is worth reporting (shipping/discount are normally absent, so they are not).
_CORE_FIELDS = (
    "invoice_number", "invoice_date", "purchase_order_number", "vendor_name", "currency",
    "subtotal", "tax_total", "grand_total", "line_items",
)


class NormalizationError(ValueError):
    """A single value could not be parsed. The message is safe to show."""


def parse_decimal(raw: str) -> tuple[Decimal, str | None]:
    """Parse a money/quantity string exactly. Returns ``(value, warning_or_None)``.

    Handles ``$1,234.50``, ``USD 1 234,50``, ``1.234,50``, ``(50.00)`` (negative) and ``-5``.
    Never goes through ``float``. Raises :class:`NormalizationError` if it is not a number.
    """
    s = raw.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    s = re.sub(r"^[A-Za-z]{3}\s*|\s*[A-Za-z]{3}$", "", s)  # leading/trailing ISO code, e.g. "USD 5"
    s = s.replace(" ", "").replace(" ", "").replace("'", "")
    s = s.strip(_CURRENCY_SYMBOLS)
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    s = s.strip(_CURRENCY_SYMBOLS)
    if not s or not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        raise NormalizationError(f"{raw!r} is not a valid number")

    warning: str | None = None
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal_sep = "." if last_dot > last_comma else ","
    elif last_comma >= 0:
        digits_after = len(s) - last_comma - 1
        if s.count(",") == 1 and digits_after != 3:
            decimal_sep = ","
        else:
            decimal_sep = None  # thousands separators only
            if s.count(",") == 1:
                warning = f"{raw!r}: a single comma followed by 3 digits was read as a thousands separator."
    elif last_dot >= 0:
        decimal_sep = "." if s.count(".") == 1 else None  # several dots => European thousands
    else:
        decimal_sep = None

    if decimal_sep:
        whole, _, frac = s.rpartition(decimal_sep)
        whole = re.sub(r"[.,]", "", whole)
        s = f"{whole or '0'}.{frac}"
    else:
        s = re.sub(r"[.,]", "", s)
    try:
        value = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise NormalizationError(f"{raw!r} is not a valid number") from exc
    return (-value if negative else value), warning


def parse_date(raw: str) -> date:
    """ISO ``YYYY-MM-DD`` first, then unambiguous textual formats. Numeric ``03/04/2024`` is refused."""
    s = raw.strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise NormalizationError(f"{raw!r} is not an unambiguous date (expected YYYY-MM-DD)")


def _money(raw: str | None, label: str, warnings: list[str], *, quantum: Decimal = CENT) -> Decimal | None:
    """Parse and explicitly quantize a money value; extra precision is reported, never silently dropped."""
    if raw is None:
        return None
    try:
        value, warning = parse_decimal(raw)
    except NormalizationError as exc:
        warnings.append(f"{label}: {exc}; value ignored.")
        return None
    if warning:
        warnings.append(f"{label}: {warning}")
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if quantized != value:
        warnings.append(f"{label}: {raw!r} has more than {abs(quantum.as_tuple().exponent)} decimal places; rounded to {quantized}.")
    return quantized


def _quantity(raw: str | None, label: str, warnings: list[str]) -> Decimal | None:
    if raw is None:
        return None
    try:
        value, warning = parse_decimal(raw)
    except NormalizationError as exc:
        warnings.append(f"{label}: {exc}; value ignored.")
        return None
    if warning:
        warnings.append(f"{label}: {warning}")
    return value


def _date(raw: str | None, label: str, warnings: list[str]) -> date | None:
    if raw is None:
        return None
    try:
        return parse_date(raw)
    except NormalizationError as exc:
        warnings.append(f"{label}: {exc}; value ignored.")
        return None


def normalize_extraction(extraction: InvoiceExtraction) -> tuple[NormalizedInvoice, list[str]]:
    """Convert strings to Decimal/date. Returns the typed invoice and a list of warnings."""
    warnings: list[str] = []

    currency = extraction.currency.upper() if extraction.currency else None
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        warnings.append(f"currency: {extraction.currency!r} is not a 3-letter ISO 4217 code; value ignored.")
        currency = None

    email = extraction.vendor_email
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        warnings.append(f"vendor_email: {email!r} does not look like an email address.")

    items: list[NormalizedLineItem] = []
    for i, li in enumerate(extraction.line_items, 1):
        label = f"line_items[{i}]"
        items.append(NormalizedLineItem(
            description=li.description,
            item_code=li.item_code,
            quantity=_quantity(li.quantity, f"{label}.quantity", warnings),
            unit_price=_money(li.unit_price, f"{label}.unit_price", warnings, quantum=UNIT_PRICE_QUANTUM),
            line_total=_money(li.line_total, f"{label}.line_total", warnings),
        ))

    normalized = NormalizedInvoice(
        invoice_number=extraction.invoice_number,
        invoice_date=_date(extraction.invoice_date, "invoice_date", warnings),
        due_date=_date(extraction.due_date, "due_date", warnings),
        purchase_order_number=extraction.purchase_order_number,
        vendor_name=extraction.vendor_name,
        vendor_address=extraction.vendor_address,
        vendor_tax_id=extraction.vendor_tax_id,
        vendor_email=email,
        currency=currency,
        **{f: _money(getattr(extraction, f), f, warnings) for f in _MONEY_FIELDS},
        line_items=items,
        payment_terms=extraction.payment_terms,
        notes=list(extraction.extraction_notes),
    )

    # "missing" is decided from the actual values, not from the model's own claim.
    claimed = {f for f in extraction.missing_fields if f in NormalizedInvoice.model_fields}
    candidates = set(_CORE_FIELDS) | claimed
    normalized.missing_fields = sorted(
        f for f in candidates if not getattr(normalized, f)  # None or empty list
    )
    return normalized, warnings


# ----------------------------------------------------------------------------- audit against source


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _amount_in_text(value: Decimal, digits_text: str) -> bool:
    """Is ``value`` printed in the document (ignoring thousands commas and currency symbols)?"""
    forms = {f"{abs(value):.2f}"}
    if value == value.to_integral_value():
        forms.add(f"{abs(value):.0f}")
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", digits_text) for f in forms)


def audit_against_source(
    extraction: InvoiceExtraction, normalized: NormalizedInvoice, source_text: str
) -> tuple[list[VerifiedEvidence], list[str]]:
    """Cross-check model output against the document text it came from.

    This catches fabricated numbers/identifiers. It is a weak signal (a number can appear in the
    text without being the right number), so mismatches are warnings for the reviewer, not proof.
    """
    warnings: list[str] = []
    squashed = _squash(source_text)
    digits_text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", source_text)  # drop thousands commas

    evidence = [
        VerifiedEvidence(field=e.field, snippet=e.snippet, found_in_source=_squash(e.snippet) in squashed)
        for e in extraction.evidence
        if isinstance(e, EvidenceSnippet) and e.snippet
    ]
    for ev in evidence:
        if not ev.found_in_source:
            warnings.append(f"Evidence for {ev.field!r} was not found verbatim in the document: {ev.snippet[:60]!r}.")

    for name in ("invoice_number", "purchase_order_number"):
        value = getattr(normalized, name)
        if value and _squash(value) not in squashed:
            warnings.append(f"{name} {value!r} does not appear in the document text; it may be wrong or invented.")

    for name in _MONEY_FIELDS:
        value = getattr(normalized, name)
        if value is not None and not _amount_in_text(value, digits_text):
            warnings.append(f"{name} {value} does not appear in the document text; verify it.")
    for i, li in enumerate(normalized.line_items, 1):
        if li.line_total is not None and not _amount_in_text(li.line_total, digits_text):
            warnings.append(f"line_items[{i}].line_total {li.line_total} does not appear in the document text; verify it.")
    return evidence, warnings
