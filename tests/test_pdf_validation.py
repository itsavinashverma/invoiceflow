import pymupdf
import pytest

from invoiceflow.exceptions import PdfValidationError
from invoiceflow.utils.pdf_security import validate_pdf_upload


def make_pdf(pages: int = 1, text: str = "hello") -> bytes:
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


def code_of(*args, **kwargs) -> str:
    with pytest.raises(PdfValidationError) as exc:
        validate_pdf_upload(*args, **kwargs)
    assert str(exc.value)  # always a human-readable message
    return exc.value.code


def test_valid_pdf_passes():
    info = validate_pdf_upload(make_pdf(2), "inv.pdf", content_type="application/pdf")
    assert info.page_count == 2 and len(info.sha256) == 64 and info.warnings == []


@pytest.mark.parametrize("name", ["inv.txt", "inv", "inv.pdf.exe", "../inv.pdf.png", "inv.PDF.zip"])
def test_bad_extension_rejected(name):
    assert code_of(make_pdf(), name) == "bad_extension"


@pytest.mark.parametrize("name", ["INV.PDF", "C:\\fake\\dir\\inv.pdf", "../../etc/inv.pdf"])
def test_extension_check_uses_basename_case_insensitively(name):
    assert validate_pdf_upload(make_pdf(), name).page_count == 1


def test_filename_not_trusted_signature_checked():
    assert code_of(b"<html>not a pdf</html>", "inv.pdf") == "bad_signature"


def test_mime_type_not_trusted_alone():
    assert code_of(make_pdf(), "inv.pdf", content_type="text/html") == "bad_content_type"
    # A PDF-claiming MIME type does not rescue fake content either.
    assert code_of(b"MZ\x90\x00 fake exe", "inv.pdf", content_type="application/pdf") == "bad_signature"
    assert validate_pdf_upload(make_pdf(), "inv.pdf", content_type="application/octet-stream").page_count == 1


def test_empty_file_rejected():
    assert code_of(b"", "inv.pdf") == "empty_file"


def test_size_limit():
    data = make_pdf()
    assert code_of(data, "inv.pdf", max_bytes=len(data) - 1) == "file_too_large"
    assert validate_pdf_upload(data, "inv.pdf", max_bytes=len(data)).size_bytes == len(data)


def test_page_limit():
    data = make_pdf(3)
    assert code_of(data, "inv.pdf", max_pages=2) == "too_many_pages"
    assert validate_pdf_upload(data, "inv.pdf", max_pages=3).page_count == 3


def test_default_limits_are_10mb_and_10_pages():
    assert code_of(make_pdf(11), "inv.pdf") == "too_many_pages"
    assert validate_pdf_upload(make_pdf(10), "inv.pdf").page_count == 10


def test_corrupt_pdf_with_valid_header_rejected():
    assert code_of(b"%PDF-1.7\n" + b"\x00\xff garbage " * 50, "inv.pdf") == "corrupt_pdf"


def test_password_protected_pdf_rejected():
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "secret")
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    assert code_of(data, "inv.pdf") == "encrypted"


def test_active_content_is_flagged_not_executed():
    data = make_pdf() + b"\n% /JavaScript /Launch /OpenAction /EmbeddedFile\n"
    info = validate_pdf_upload(data, "inv.pdf")
    assert info.page_count == 1
    assert len(info.warnings) == 1 and "never executed" in info.warnings[0]
    for label in ("JavaScript", "launch actions", "embedded files", "automatic open actions"):
        assert label in info.warnings[0]


def test_validation_writes_nothing_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    validate_pdf_upload(make_pdf(), "inv.pdf")
    assert list(tmp_path.iterdir()) == []
