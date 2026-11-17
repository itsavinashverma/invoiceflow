"""TEST DOUBLES. This file is for automated tests only and is deliberately NOT part of the ``invoiceflow``
package, so the application cannot import it.

``FakeAiProvider`` stands in for Gemini with controlled answers. It cannot be mistaken for the real thing:
its provider name is ``TEST-DOUBLE``, its model is ``TEST-DOUBLE-NOT-GEMINI``, every answer carries a
warning saying so, and ``InvoicePipeline`` refuses any provider marked ``is_test_double`` unless pytest is running.
"""

from __future__ import annotations

from collections.abc import Callable

from invoiceflow.ai.invoice_extractor import AiCallResult
from invoiceflow.schemas import InvoiceExtraction

TEST_DOUBLE_WARNING = "Produced by a TEST DOUBLE, not by Gemini."


class FakeAiProvider:
    name = "TEST-DOUBLE"
    is_test_double = True
    MODEL = "TEST-DOUBLE-NOT-GEMINI"

    def __init__(
        self,
        extraction: InvoiceExtraction | Callable[[str], InvoiceExtraction] | None = None,
        *,
        fail: tuple[str, str, str] | None = None,  # (status, error_code, message), e.g. ("unavailable", "service_unavailable", "...")
        crash: Exception | None = None,  # simulate a programming error inside the provider
    ) -> None:
        self._extraction, self._fail, self._crash = extraction, fail, crash
        self.calls: list[dict] = []

    def extract(self, document_text: str, *, source_method: str) -> AiCallResult:
        self.calls.append({"text": document_text, "source_method": source_method})
        if self._crash is not None:
            raise self._crash
        base = dict(provider=self.name, model=self.MODEL, text_used=document_text, is_test_double=True)
        if self._fail is not None:
            status, code, message = self._fail
            return AiCallResult(status=status, error_code=code, error_message=message, attempts=3, **base)  # type: ignore[arg-type]
        extraction = self._extraction(document_text) if callable(self._extraction) else self._extraction
        assert extraction is not None, "FakeAiProvider needs an extraction or a failure"
        return AiCallResult(
            status="success",
            extraction=extraction,
            attempts=1,
            raw_response=extraction.model_dump_json(),
            warnings=[TEST_DOUBLE_WARNING],
            **base,
        )


def ground_truth(gen_module, file_name: str, **overrides) -> InvoiceExtraction:
    """What a perfect model would extract from a generated invoice (built from the generator's data, as strings)."""
    inv = next(i for i in gen_module.build_invoices(gen_module.build_purchase_orders()) if i.file_name == file_name)
    return extraction_from_invoice(inv, **overrides)


def extraction_from_invoice(inv, **overrides) -> InvoiceExtraction:
    """A perfect extraction of any generator ``Invoice`` (including custom variants built by a test)."""
    tax = sum((a for _, _, a in inv.tax_amounts), type(inv.subtotal)("0")) if inv.taxes else None
    data = {
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date.isoformat(),
        "purchase_order_number": inv.po_number,
        "vendor_name": inv.vendor.name,
        "vendor_tax_id": inv.vendor.tax_id,
        "currency": inv.currency,
        "subtotal": str(inv.subtotal),
        "tax_total": str(tax) if tax is not None else None,
        "grand_total": str(inv.total),
        "line_items": [
            {
                "description": ln.description,
                "item_code": ln.sku,
                "quantity": str(ln.quantity),
                "unit_price": str(ln.unit_price),
                "line_total": str(ln.amount),
            }
            for ln in inv.lines
        ],
    }
    data.update(overrides)
    return InvoiceExtraction.model_validate(data)


class TextDrivenAiProvider(FakeAiProvider):
    """TEST DOUBLE that extracts ONLY what is literally printed in the text it receives (regular expressions).

    Used for the scanned-invoice scenario: if OCR had not really produced the invoice's text, this provider
    could not find the fields, so the test proves the OCR output itself carries them.
    """

    MONEY = r"[$€£]\s?([\d,]+\.\d\d)"

    def __init__(self) -> None:
        super().__init__(InvoiceExtraction())

    def extract(self, document_text: str, *, source_method: str) -> AiCallResult:
        import re
        from decimal import Decimal

        self.calls.append({"text": document_text, "source_method": source_method})

        def first(pattern: str, flags: int = 0) -> str | None:
            m = re.search(pattern, document_text, flags)
            return m.group(1).replace(",", "") if m else None

        taxes = [Decimal(a.replace(",", "")) for a in re.findall(rf"Sales tax \([\d.]+%\)\s+{self.MONEY}", document_text)]
        lines = []
        for m in re.finditer(rf"^\s*\d+\s+(\S+)\s+(.+?)\s+(\d+)\s+{self.MONEY}\s+{self.MONEY}\s*$", document_text, re.M):
            lines.append(
                {
                    "item_code": m.group(1).lstrip("_"),
                    "description": m.group(2),
                    "quantity": m.group(3),
                    "unit_price": m.group(4).replace(",", ""),
                    "line_total": m.group(5).replace(",", ""),
                }
            )
        vendor = re.search(r"^(.+?)\s+INVOICE\b", document_text, re.M)
        extraction = InvoiceExtraction.model_validate(
            {
                "invoice_number": first(r"Invoice No:\s*([A-Z0-9][\w-]+)"),
                "purchase_order_number": first(r"PO Number:\s*(PO-[\w-]+)"),
                "vendor_name": vendor.group(1).strip() if vendor else None,
                "currency": first(r"TOTAL DUE \((\w{3})\)"),
                "subtotal": first(rf"Subtotal\s+{self.MONEY}"),
                "tax_total": str(sum(taxes, Decimal("0"))) if taxes else None,
                "grand_total": first(rf"TOTAL DUE \(\w{{3}}\)\s+{self.MONEY}"),
                "line_items": lines,
            }
        )
        return AiCallResult(
            status="success",
            extraction=extraction,
            attempts=1,
            raw_response=extraction.model_dump_json(),
            warnings=[TEST_DOUBLE_WARNING],
            provider=self.name,
            model=self.MODEL,
            text_used=document_text,
            is_test_double=True,
        )
