import hashlib
from dataclasses import replace

import pymupdf
import pytest
from conftest import requires_tesseract

from invoiceflow.exceptions import NoTextExtractedError, OcrUnavailableError, PdfValidationError
from invoiceflow.extraction.ocr_extractor import ensure_tesseract, ocr_image, render_page
from invoiceflow.extraction.pdf_extractor import extract_document, meaningful_chars
from invoiceflow.schemas import ExtractionResult


def money(d):
    return f"${d:,.2f}"


def read(g):
    return g.path.read_bytes()


# ---------------------------------------------------------------- digital PDFs


def test_digital_pdf_is_readable(demo, settings):
    g = demo["standard"]
    r = extract_document(read(g), g.path.name, settings)
    assert isinstance(r, ExtractionResult)
    assert r.method == "text" and r.ocr_pages == [] and r.page_count == 1
    for expected in (g.invoice_number, g.po_number, g.vendor_name, money(g.total), money(g.subtotal)):
        assert expected in r.text
    assert r.pages[0].method == "text" and not r.pages[0].ocr_attempted
    assert g.invoice_number in r.metadata.get("title", "")
    assert r.duration_seconds >= 0
    assert r.file_sha256 == hashlib.sha256(read(g)).hexdigest()
    assert r.file_size_bytes == len(read(g))


def test_digital_pdf_needs_no_tesseract(demo, settings):
    broken = replace(settings, tesseract_cmd=r"C:\definitely\missing\tesseract.exe")
    g = demo["standard"]
    assert extract_document(read(g), g.path.name, broken).method == "text"


def test_multipage_pdf_all_pages_processed(demo, settings):
    g = demo["multipage"]
    r = extract_document(read(g), g.path.name, settings)
    assert r.page_count >= 2 and len(r.pages) == r.page_count
    assert all(p.method == "text" and p.meaningful_chars > 100 for p in r.pages)
    # Key facts are NOT all on page one: totals live on the last page.
    assert money(g.total) not in r.pages[0].text
    assert money(g.total) in r.pages[-1].text
    assert g.invoice_number in r.pages[0].text
    assert f"--- Page {r.page_count} of {r.page_count} ---" in r.text


def test_tax_separated_invoice(demo, settings):
    g = demo["tax"]
    r = extract_document(read(g), g.path.name, settings)
    assert "Subtotal" in r.text and money(g.subtotal) in r.text and money(g.total) in r.text
    for label in g.tax_labels:
        assert label in r.text
    assert len(g.tax_labels) == 3


def test_missing_po_invoice_has_no_po_reference(demo, settings):
    g = demo["missing_po"]
    r = extract_document(read(g), g.path.name, settings)
    assert g.invoice_number in r.text
    assert "PO Number" not in r.text and "PO-2024" not in r.text


# ---------------------------------------------------------------- scanned PDFs / OCR


def test_scanned_fixture_really_has_no_text_layer(demo):
    with pymupdf.open(demo["scanned"].path) as doc:
        assert doc[0].get_text().strip() == ""
        assert doc[0].get_images()


@requires_tesseract
def test_scanned_pdf_uses_ocr(demo, settings):
    g = demo["scanned"]
    r = extract_document(read(g), g.path.name, settings)
    assert r.method == "ocr" and r.ocr_pages == [1]
    page = r.pages[0]
    assert page.method == "ocr" and page.ocr_attempted and page.text_layer_chars == 0
    assert page.ocr_confidence is not None and page.ocr_confidence > 50
    squashed = r.text.replace(" ", "")
    assert g.invoice_number.split("-")[-1] in squashed  # tolerate OCR noise in the prefix
    assert g.po_number in squashed
    assert "3,335.00" in r.text  # subtotal


@requires_tesseract
def test_mixed_document_only_ocrs_needed_pages(demo, settings):
    with pymupdf.open() as combo:
        for key in ("standard", "scanned"):
            with pymupdf.open(demo[key].path) as src:
                combo.insert_pdf(src)
        data = combo.tobytes()
    r = extract_document(data, "combo.pdf", settings)
    assert r.method == "mixed" and r.ocr_pages == [2]
    assert r.pages[0].method == "text" and r.pages[1].method == "ocr"


def test_missing_tesseract_gives_actionable_error(demo, settings):
    broken = replace(settings, tesseract_cmd=r"C:\definitely\missing\tesseract.exe")
    g = demo["scanned"]
    with pytest.raises(OcrUnavailableError) as exc:
        extract_document(read(g), g.path.name, broken)
    msg = str(exc.value)
    assert "TESSERACT_CMD" in msg and "Install" in msg and "Page(s) 1" in msg
    assert exc.value.code == "ocr_unavailable"
    partial = exc.value.partial_result  # honest: shows nothing was read from the page
    assert partial is not None and partial.pages[0].method == "none"


@requires_tesseract
def test_ocr_blank_image_yields_no_text(settings):
    from PIL import Image

    ensure_tesseract(settings.tesseract_cmd)
    out = ocr_image(Image.new("L", (400, 200), 255))
    assert out.text.strip() == "" and out.confidence is None


def test_render_page_caps_image_size():
    with pymupdf.open() as doc:
        page = doc.new_page(width=14400, height=14400)  # 200 inch square page
        img = render_page(page, dpi=300, max_side_px=2000)
    assert max(img.size) <= 2000


# ---------------------------------------------------------------- bad documents


def test_blank_pdf_raises_no_text(settings):
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        data = doc.tobytes()
    with pytest.raises(NoTextExtractedError):
        extract_document(data, "blank.pdf", settings)


def test_blank_page_in_otherwise_good_pdf_is_flagged(demo, settings):
    with pymupdf.open(demo["standard"].path) as doc:
        doc.new_page()
        data = doc.tobytes()
    r = extract_document(data, "x.pdf", settings)
    assert r.page_count == 2 and r.pages[1].method == "none"
    assert any("blank" in w for w in r.warnings)


def test_extract_document_validates_first(settings):
    with pytest.raises(PdfValidationError):
        extract_document(b"hello world", "notes.pdf", settings)


def test_settings_page_limit_is_enforced(demo, settings):
    g = demo["multipage"]
    with pytest.raises(PdfValidationError) as exc:
        extract_document(read(g), g.path.name, replace(settings, max_pdf_pages=1))
    assert exc.value.code == "too_many_pages"


def test_meaningful_chars_ignores_noise():
    assert meaningful_chars(" -- \n . , ") == 0
    assert meaningful_chars("INV 42") == 5
