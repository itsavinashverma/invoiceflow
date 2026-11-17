import json
from decimal import Decimal
from pathlib import Path

import pytest

from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import PurchaseOrder
from invoiceflow.database.repositories import count_purchase_orders, list_purchase_orders
from invoiceflow.database.seed import DEFAULT_PO_FILE, SeedDataError, load_purchase_orders, seed_purchase_orders
from invoiceflow.extraction.pdf_extractor import extract_document
from invoiceflow.services.normalizer import normalize_vendor_name


@pytest.fixture(scope="module")
def seed_items():
    return load_purchase_orders()


def test_seed_file_has_realistic_variety(seed_items):
    assert len(seed_items) >= 10
    assert len({p.po_number for p in seed_items}) == len(seed_items)
    assert len({normalize_vendor_name(p.vendor_name) for p in seed_items}) == len(seed_items)  # distinct vendors
    assert {p.status for p in seed_items} == {"OPEN", "CLOSED"}
    assert {"USD", "EUR", "GBP"} <= {p.currency for p in seed_items}
    assert len({p.tolerance_percent for p in seed_items}) >= 5
    assert all(p.total_amount > 0 for p in seed_items)


def test_seed_loads_into_empty_database(db_engine, seed_items):
    with session_scope(db_engine) as s:
        result = seed_purchase_orders(s)
    assert len(result.created) == len(seed_items) and result.already_present == []
    with session_scope(db_engine) as s:
        assert count_purchase_orders(s) == len(seed_items)
        po = next(p for p in list_purchase_orders(s) if p.po_number == "PO-2024-1001")
        assert po.total_amount == Decimal("1216.00") and po.tolerance_percent == Decimal("2.00")
        assert po.normalized_vendor_name == "acme industrial supplies"
        assert {p.status for p in list_purchase_orders(s)} == {"OPEN", "CLOSED"}


def test_reseeding_creates_no_duplicates(db_engine, seed_items):
    for _ in range(3):
        with session_scope(db_engine) as s:
            seed_purchase_orders(s)
    with session_scope(db_engine) as s:
        assert count_purchase_orders(s) == len(seed_items)
    with session_scope(db_engine) as s:
        again = seed_purchase_orders(s)
    assert again.created == [] and len(again.already_present) == len(seed_items)


def test_reseeding_never_overwrites_edited_rows_and_restores_missing_ones(db_engine, seed_items):
    with session_scope(db_engine) as s:
        seed_purchase_orders(s)
    with session_scope(db_engine) as s:
        edited = s.query(PurchaseOrder).filter_by(po_number="PO-2024-1001").one()
        edited.status, edited.tolerance_percent = "CLOSED", Decimal("9.99")
        s.delete(s.query(PurchaseOrder).filter_by(po_number="PO-2024-1002").one())
    with session_scope(db_engine) as s:
        result = seed_purchase_orders(s)
    assert result.created == ["PO-2024-1002"]
    with session_scope(db_engine) as s:
        po = s.query(PurchaseOrder).filter_by(po_number="PO-2024-1001").one()
        assert po.status == "CLOSED" and po.tolerance_percent == Decimal("9.99")  # user edits survive


def test_seed_data_survives_restart(tmp_path):
    from invoiceflow.database.connection import create_db_engine, init_db

    url = f"sqlite:///{(tmp_path / 'restart.db').as_posix()}"
    e1 = create_db_engine(url)
    init_db(e1)
    with session_scope(e1) as s:
        seed_purchase_orders(s)
    e1.dispose()
    e2 = create_db_engine(url)  # "application restart"
    init_db(e2)
    with session_scope(e2) as s:
        assert count_purchase_orders(s) >= 10
    e2.dispose()


# ------------------------------------------------------------------ consistency with the generated invoices


def test_committed_seed_file_matches_generator(gen_module, tmp_path):
    gen_module.generate_all(tmp_path)
    fresh = json.loads((tmp_path / "purchase_orders" / "purchase_orders.json").read_text(encoding="utf-8"))
    committed = json.loads(DEFAULT_PO_FILE.read_text(encoding="utf-8"))
    assert fresh == committed, "sample_data is stale: run python scripts/generate_demo_data.py"


def test_every_invoice_is_consistent_with_its_po(gen_module, seed_items):
    pos = {p.po_number: p for p in seed_items}
    invoices = gen_module.build_invoices(gen_module.build_purchase_orders())
    assert len(invoices) >= 10
    for inv in invoices:
        if inv.po_number is None:
            continue
        po = pos[inv.po_number]
        assert inv.currency == po.currency, inv.file_name
        assert normalize_vendor_name(inv.vendor.name) == normalize_vendor_name(po.vendor_name), inv.file_name
        assert inv.subtotal <= po.total_amount, inv.file_name
        if inv.file_name != "invoice_massive_partial.pdf":
            assert inv.subtotal == po.total_amount, inv.file_name  # full billing matches the PO exactly
    closed = {p.po_number for p in seed_items if p.status == "CLOSED"}
    assert {i.po_number for i in invoices} & closed  # at least one invoice targets a CLOSED PO


def test_generated_invoice_pdfs_are_readable_with_correct_currency(demo, settings):
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    digital = [g for g in demo["all"] if not g.scanned]
    assert len(demo["all"]) >= 10
    for g in digital:
        r = extract_document(g.path.read_bytes(), g.path.name, settings)
        assert g.invoice_number in r.text, g.path.name
        assert f"{symbols[g.currency]}{g.total:,.2f}" in r.text, g.path.name
        assert f"({g.currency})" in r.text or f"Currency: {g.currency}" in r.text.replace("\n", " ") or g.currency in r.text


# ------------------------------------------------------------------ bad seed files


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pos.json"
    p.write_text(content, encoding="utf-8")
    return p


def entry(**over):
    base = {
        "po_number": "PO-X",
        "vendor": {"name": "V Ltd"},
        "currency": "usd",
        "total": "10.50",
        "tolerance_percent": "2.00",
        "status": "OPEN",
        "issue_date": "2024-01-02",
    }
    base.update(over)
    return base


def test_seed_loader_accepts_floats_in_json_as_exact_strings(tmp_path):
    p = write(tmp_path, '[{"po_number":"PO-X","vendor":{"name":"V"},"currency":"usd","total":0.10,"issue_date":"2024-01-02"}]')
    (item,) = load_purchase_orders(p)
    assert item.total_amount == Decimal("0.10") and item.currency == "USD"


@pytest.mark.parametrize(
    "content, match",
    [
        ("not json", "not valid JSON"),
        ("[]", "non-empty"),
        ("{}", "non-empty"),
        (json.dumps([entry(), entry()]), "Duplicate"),
        (json.dumps([entry(currency="DOLLARS")]), "invalid"),
        (json.dumps([entry(total="-5")]), "invalid"),
        (json.dumps([entry(status="PENDING")]), "invalid"),
        (json.dumps([entry(tolerance_percent="150")]), "invalid"),
        (json.dumps([{"po_number": "PO-X"}]), "invalid"),
    ],
)
def test_bad_seed_files_are_rejected_with_clear_errors(tmp_path, content, match):
    with pytest.raises(SeedDataError, match=match):
        load_purchase_orders(write(tmp_path, content))


def test_missing_seed_file_says_how_to_generate_it(tmp_path):
    with pytest.raises(SeedDataError, match="generate_demo_data"):
        load_purchase_orders(tmp_path / "nope.json")
