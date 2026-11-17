from datetime import date
from decimal import Decimal

import pytest

from invoiceflow.schemas import InvoiceExtraction, LineItemExtraction
from invoiceflow.services.normalizer import (
    NormalizationError,
    audit_against_source,
    normalize_extraction,
    parse_date,
    parse_decimal,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1301.12", "1301.12"),
        ("1,234.50", "1234.50"),
        ("$1,234.50", "1234.50"),
        ("USD 1 234,50", "1234.50"),
        ("1.234,50", "1234.50"),
        ("12,50", "12.50"),
        ("1.234.567", "1234567"),
        ("1,234,567.89", "1234567.89"),
        ("(50.00)", "-50.00"),
        ("-5", "-5"),
        ("€ 7,5", "7.5"),
        ("0.10", "0.10"),
    ],
)
def test_parse_decimal(raw, expected):
    value, _ = parse_decimal(raw)
    assert isinstance(value, Decimal) and value == Decimal(expected)


def test_parse_decimal_is_exact_not_float():
    a, _ = parse_decimal("0.1")
    b, _ = parse_decimal("0.2")
    assert a + b == Decimal("0.3")  # would be 0.30000000000000004 with floats


def test_ambiguous_single_comma_is_flagged():
    value, warning = parse_decimal("1,234")
    assert value == Decimal("1234") and warning and "thousands" in warning


@pytest.mark.parametrize("raw", ["", "abc", "1e5", "12 apples", "--", "1.2.3,4,5x", "$"])
def test_parse_decimal_rejects_garbage(raw):
    with pytest.raises(NormalizationError):
        parse_decimal(raw)


def test_parse_date():
    assert parse_date("2024-09-20") == date(2024, 9, 20)
    assert parse_date("20 Sep 2024") == date(2024, 9, 20)
    assert parse_date("Sep 20, 2024") == date(2024, 9, 20)
    with pytest.raises(NormalizationError):
        parse_date("03/04/2024")  # day/month order unknowable
    with pytest.raises(NormalizationError):
        parse_date("2024-13-40")


def test_normalize_builds_decimals_and_dates():
    ex = InvoiceExtraction(
        invoice_number="INV-1", invoice_date="2024-09-20", currency="usd", subtotal="1,216.00",
        tax_total="85.12", grand_total="1301.12",
        line_items=[LineItemExtraction(description="x", quantity="12", unit_price="45.5", line_total="546.00")],
    )
    n, warnings = normalize_extraction(ex)
    assert warnings == []
    assert n.invoice_date == date(2024, 9, 20) and n.currency == "USD"
    assert n.subtotal == Decimal("1216.00") and n.grand_total == Decimal("1301.12")
    li = n.line_items[0]
    assert li.quantity == Decimal("12") and li.unit_price == Decimal("45.5000") and li.line_total == Decimal("546.00")
    assert all(not isinstance(v, float) for v in n.model_dump().values())


def test_excess_precision_is_rounded_half_up_with_warning():
    n, warnings = normalize_extraction(InvoiceExtraction(subtotal="10.005"))
    assert n.subtotal == Decimal("10.01")
    assert any("rounded" in w for w in warnings)


def test_unparseable_values_become_none_with_warnings():
    n, warnings = normalize_extraction(
        InvoiceExtraction(subtotal="about 100", invoice_date="yesterday", currency="US$", vendor_email="nope")
    )
    assert n.subtotal is None and n.invoice_date is None and n.currency is None
    joined = " ".join(warnings)
    for token in ("subtotal", "invoice_date", "currency", "vendor_email"):
        assert token in joined


def test_missing_fields_computed_from_values_not_model_claims():
    ex = InvoiceExtraction(invoice_number="A1", missing_fields=["invoice_number", "due_date", "not_a_field"])
    n, _ = normalize_extraction(ex)
    assert "invoice_number" not in n.missing_fields  # claimed missing, but present
    assert "due_date" in n.missing_fields and "not_a_field" not in n.missing_fields
    assert {"purchase_order_number", "grand_total", "line_items"} <= set(n.missing_fields)


def test_audit_flags_invented_values():
    text = "Invoice No: A-100\nPO Number: PO-7\nSubtotal $1,000.00\nTOTAL $1,070.00"
    ex = InvoiceExtraction(invoice_number="A-100", purchase_order_number="PO-9", subtotal="1000.00", grand_total="1170.00")
    n, _ = normalize_extraction(ex)
    _, warnings = audit_against_source(ex, n, text)
    joined = " ".join(warnings)
    assert "PO-9" in joined and "1170.00" in joined
    assert "A-100" not in joined and "subtotal" not in joined


def test_audit_evidence_verification():
    ex = InvoiceExtraction.model_validate({
        "invoice_number": "A-100",
        "evidence": [{"field": "invoice_number", "snippet": "Invoice   No:  A-100"}, {"field": "grand_total", "snippet": "TOTAL $9.99"}],
    })
    n, _ = normalize_extraction(ex)
    evidence, warnings = audit_against_source(ex, n, "Invoice No: A-100\nTOTAL $1,070.00")
    assert [e.found_in_source for e in evidence] == [True, False]
    assert any("grand_total" in w for w in warnings)
