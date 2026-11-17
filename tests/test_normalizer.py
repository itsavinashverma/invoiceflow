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
        invoice_number="INV-1",
        invoice_date="2024-09-20",
        currency="usd",
        subtotal="1,216.00",
        tax_total="85.12",
        grand_total="1301.12",
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


@pytest.mark.parametrize(
    "a, b",
    [
        ("ACME Industrial Supplies, Ltd.", "Acme Industrial Supplies Limited"),
        ("Acme & Sons Inc", "ACME and Sons"),
        ("Müller Söhne GmbH", "Muller Sohne"),
        ("  Globex   IT  Services LLC ", "globex it services"),
    ],
)
def test_vendor_names_normalise_to_the_same_key(a, b):
    from invoiceflow.services.normalizer import normalize_vendor_name

    assert normalize_vendor_name(a) == normalize_vendor_name(b) is not None


@pytest.mark.parametrize(
    "a, b, kind",
    [
        ("Acme Industrial Supplies Ltd", "ACME INDUSTRIAL SUPPLIES, LIMITED", "exact"),
        ("Acme Inc.", "ACME Incorporated", "exact"),
        ("Vandelay Import Export S.L.", "vandelay import export sl", "exact"),
        ("Acme L.L.C.", "Acme LLC", "exact"),
        ("Northwind Office Co", "Northwind Office Company", "exact"),
        ("Acme Pvt Ltd", "ACME Private Limited", "exact"),
        ("Acme Industrial Supplies", "Acme Industrial Supplies Ltd", "legal_form_omitted"),
        ("Acme Inc", "Acme LLC", "different_legal_form"),
        ("Acme Ltd", "Acme Co Ltd", "different_legal_form"),
        ("Acme Industrial Supplies Ltd", "Acme Industries Ltd", "different"),
        ("Acme Industrial", "Acme Industrial Supplies", "different"),
        ("Globex IT Services LLC", "Acme Industrial Supplies Ltd", "different"),
        ("Acme", None, "missing"),
        ("  ", "Acme", "missing"),
    ],
)
def test_compare_vendors_is_conservative(a, b, kind):
    from invoiceflow.services.normalizer import compare_vendors

    got = compare_vendors(a, b)
    assert got.kind == kind
    assert got.matches == (kind in ("exact", "legal_form_omitted"))
    assert compare_vendors(b, a).kind == kind  # symmetric


def test_parse_vendor_preserves_the_original_name():
    from invoiceflow.services.normalizer import parse_vendor_name

    p = parse_vendor_name("  ACME  Industrial Supplies, Ltd. ")
    assert p.original == "  ACME  Industrial Supplies, Ltd. "  # never modified
    assert (p.base, p.legal_form) == ("acme industrial supplies", "ltd")
    assert parse_vendor_name("Acme Co Ltd").legal_form == "co ltd"  # several trailing legal words are kept together


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("PO-2024-1001", "po-2024-1001", True),
        ("PO-2024-1001", " PO-2024-1001 ", True),
        ("PO 2024-1001", "PO2024-1001", True),
        ("PO‑2024–1001", "PO-2024-1001", True),
        ("PO-1-001", "PO-10-01", False),
        ("PO-2024-1001", "PO-2024-1002", False),
        ("PO-2024-1001", "PO-2024-1OO1", False),
    ],
)
def test_po_number_normalisation(a, b, same):
    from invoiceflow.services.normalizer import normalize_po_number

    assert (normalize_po_number(a) == normalize_po_number(b)) is same
    assert normalize_po_number(None) is None and normalize_po_number("  ") is None


def test_vendor_name_edge_cases():
    from invoiceflow.services.normalizer import normalize_vendor_name

    assert normalize_vendor_name(None) is None and normalize_vendor_name("   ") is None
    assert normalize_vendor_name("Co") == "co"  # a name that is only a suffix is kept
    assert normalize_vendor_name("Acme") != normalize_vendor_name("Acme Two")


def test_content_hash_is_stable_and_sensitive():
    from invoiceflow.services.normalizer import compute_content_hash, normalize_invoice_number

    base = ("Acme Ltd", "INV-1", Decimal("10.00"), date(2024, 1, 1))
    assert compute_content_hash(*base) == compute_content_hash("ACME Limited", " inv-1", Decimal("10.0"), date(2024, 1, 1))
    for changed in (
        ("Acme Ltd", "INV-2", Decimal("10.00"), date(2024, 1, 1)),
        ("Acme Ltd", "INV-1", Decimal("10.01"), date(2024, 1, 1)),
        ("Acme Ltd", "INV-1", Decimal("10.00"), date(2024, 1, 2)),
        ("Other", "INV-1", Decimal("10.00"), date(2024, 1, 1)),
    ):
        assert compute_content_hash(*changed) != compute_content_hash(*base)
    assert normalize_invoice_number(" INV - 1 ") == "inv-1" and normalize_invoice_number(None) is None


def test_audit_flags_invented_values():
    text = "Invoice No: A-100\nPO Number: PO-7\nSubtotal $1,000.00\nTOTAL $1,070.00"
    ex = InvoiceExtraction(invoice_number="A-100", purchase_order_number="PO-9", subtotal="1000.00", grand_total="1170.00")
    n, _ = normalize_extraction(ex)
    _, warnings = audit_against_source(ex, n, text)
    joined = " ".join(warnings)
    assert "PO-9" in joined and "1170.00" in joined
    assert "A-100" not in joined and "subtotal" not in joined


def test_audit_evidence_verification():
    ex = InvoiceExtraction.model_validate(
        {
            "invoice_number": "A-100",
            "evidence": [
                {"field": "invoice_number", "snippet": "Invoice   No:  A-100"},
                {"field": "grand_total", "snippet": "TOTAL $9.99"},
            ],
        }
    )
    n, _ = normalize_extraction(ex)
    evidence, warnings = audit_against_source(ex, n, "Invoice No: A-100\nTOTAL $1,070.00")
    assert [e.found_in_source for e in evidence] == [True, False]
    assert any("grand_total" in w for w in warnings)
