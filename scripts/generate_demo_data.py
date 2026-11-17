"""Generate synthetic purchase orders and invoice PDFs (ReportLab).

Invoices are built *from* the purchase-order data so the two stay consistent. The PDFs are real
documents: extraction results are never hardcoded, tests run the generated files through the
actual pipeline. Output is deterministic (fixed data, fixed PDF metadata, seeded noise).

Usage:
    python scripts/generate_demo_data.py [--output-dir sample_data]
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

CENT = Decimal("0.01")
BILL_TO = ("Contoso Manufacturing Inc.", "400 Industrial Way", "Dayton, OH 45402", "Accounts Payable")


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Vendor:
    name: str
    address: tuple[str, ...]
    tax_id: str


@dataclass(frozen=True)
class Line:
    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return q(self.quantity * self.unit_price)


@dataclass(frozen=True)
class PurchaseOrder:
    po_number: str
    vendor: Vendor
    issue_date: date
    lines: tuple[Line, ...]
    currency: str = "USD"

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal("0"))

    def to_json(self) -> dict:
        return {
            "po_number": self.po_number,
            "vendor": {"name": self.vendor.name, "address": list(self.vendor.address), "tax_id": self.vendor.tax_id},
            "currency": self.currency,
            "issue_date": self.issue_date.isoformat(),
            "lines": [
                {"line_no": i, "sku": ln.sku, "description": ln.description,
                 "quantity": str(ln.quantity), "unit_price": str(ln.unit_price)}
                for i, ln in enumerate(self.lines, 1)
            ],
            "total": str(self.total),
        }


@dataclass(frozen=True)
class Invoice:
    file_name: str
    invoice_number: str
    vendor: Vendor
    invoice_date: date
    lines: tuple[Line, ...]
    po_number: str | None
    taxes: tuple[tuple[str, Decimal], ...]  # (label, rate) applied to the subtotal
    scanned: bool = False
    notes: str = ""

    @property
    def subtotal(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal("0"))

    @property
    def tax_amounts(self) -> list[tuple[str, Decimal, Decimal]]:
        return [(label, rate, q(self.subtotal * rate)) for label, rate in self.taxes]

    @property
    def total(self) -> Decimal:
        return self.subtotal + sum((a for _, _, a in self.tax_amounts), Decimal("0"))


@dataclass
class GeneratedInvoice:
    """Ground truth about a generated file, for tests and demos (not used by extraction)."""

    path: Path
    invoice_number: str
    po_number: str | None
    vendor_name: str
    subtotal: Decimal
    total: Decimal
    scanned: bool
    tax_labels: list[str] = field(default_factory=list)


ACME = Vendor("Acme Industrial Supplies Ltd", ("12 Foundry Road", "Pittsburgh, PA 15201"), "US-47-1234567")
NORTHWIND = Vendor("Northwind Office Co", ("88 Commerce Street", "Columbus, OH 43215"), "US-31-7654321")
GLOBEX = Vendor("Globex IT Services LLC", ("501 Technology Drive", "Austin, TX 78701"), "US-74-2468135")
INITECH = Vendor("Initech Facilities Management", ("7 Harbor Plaza", "Cleveland, OH 44114"), "US-34-1357924")

_OFFICE_ITEMS = [
    ("NW-PAP-A4", "A4 copy paper, 5-ream carton", "28.40"), ("NW-PEN-BLK", "Ballpoint pens, black, box of 50", "11.75"),
    ("NW-FLD-LTR", "Manila file folders, box of 100", "14.20"), ("NW-STP-STD", "Heavy-duty stapler", "19.90"),
    ("NW-TNR-85A", "Toner cartridge 85A, black", "62.35"), ("NW-NTB-SPR", "Spiral notebooks, pack of 10", "16.80"),
    ("NW-MKR-WHT", "Whiteboard markers, set of 12", "9.45"), ("NW-LBL-AVY", "Address labels, 1000 count", "13.10"),
    ("NW-ENV-C5", "C5 envelopes, box of 500", "21.60"), ("NW-CLP-BND", "Binder clips, assorted, box of 144", "7.25"),
    ("NW-DSK-ORG", "Desktop organiser, mesh", "17.95"), ("NW-CAL-WLL", "Wall calendar, 12 month", "8.60"),
    ("NW-TAP-CLR", "Packing tape, clear, 6-roll", "12.30"), ("NW-SCS-8IN", "Office scissors, 8 inch", "6.40"),
    ("NW-HLT-YLW", "Highlighters, yellow, box of 12", "8.15"), ("NW-PST-3X3", "Sticky notes 3x3, 12 pads", "10.55"),
    ("NW-CBL-USB", "USB-C cable, 2 m", "9.99"), ("NW-MOU-WRL", "Wireless mouse", "18.75"),
    ("NW-KBD-STD", "Standard USB keyboard", "24.50"), ("NW-MON-ARM", "Monitor arm, single", "54.00"),
    ("NW-CHR-MAT", "Chair mat, 36x48 in", "46.80"), ("NW-LMP-LED", "LED desk lamp", "27.30"),
    ("NW-SHR-CRS", "Cross-cut shredder, 10 sheet", "89.00"), ("NW-WST-BIN", "Wastebasket, 28 qt", "11.20"),
    ("NW-FRM-A4", "Certificate frame, A4", "13.85"), ("NW-PNC-HOL", "3-hole punch, 20 sheet", "22.40"),
    ("NW-RUL-30C", "Ruler, 30 cm", "2.15"), ("NW-ERS-WHT", "Erasers, pack of 20", "4.30"),
    ("NW-GLU-STK", "Glue sticks, pack of 12", "6.95"), ("NW-TIE-ZIP", "Cable ties, 200 pack", "5.80"),
    ("NW-BAT-AA", "AA batteries, 24 pack", "15.60"), ("NW-BAT-AAA", "AAA batteries, 24 pack", "14.90"),
    ("NW-CLN-WIP", "Screen cleaning wipes, 100", "7.70"), ("NW-HDN-PHN", "Headset, USB, mono", "33.25"),
    ("NW-WBD-90", "Whiteboard 90x60 cm", "58.40"), ("NW-PIN-BRD", "Pin board, cork, 60x45 cm", "26.10"),
    ("NW-DVD-SLV", "Document wallets, pack of 50", "12.65"), ("NW-LTR-TRY", "Letter tray, 3-tier", "20.95"),
    ("NW-ROL-BUB", "Bubble wrap roll, 50 m", "31.40"), ("NW-FLS-DRV", "USB flash drive, 64 GB", "8.99"),
    ("NW-CHR-TLR", "Task chair, ergonomic", "149.00"), ("NW-PRT-LBL", "Label printer ribbon", "18.20"),
    ("NW-PLN-WKL", "Weekly planner, 2025", "10.40"),
]


def build_purchase_orders() -> dict[str, PurchaseOrder]:
    D = Decimal
    pos = [
        PurchaseOrder("PO-2024-1001", ACME, date(2024, 9, 2), (
            Line("AC-HOS-12", "Hydraulic hose assembly, 12 in", D("12"), D("45.50")),
            Line("AC-BRK-ST", "Steel mounting bracket", D("40"), D("8.75")),
            Line("AC-GLV-PR", "Safety gloves (pair)", D("100"), D("3.20")),
        )),
        PurchaseOrder("PO-2024-1002", NORTHWIND, date(2024, 9, 9), tuple(
            Line(sku, desc, D(str(3 + (i * 7) % 12)), D(price)) for i, (sku, desc, price) in enumerate(_OFFICE_ITEMS)
        )),
        PurchaseOrder("PO-2024-1003", GLOBEX, date(2024, 9, 16), (
            Line("GX-CON-HR", "Cloud migration consulting (hours)", D("80"), D("125.00")),
            Line("GX-HST-MO", "Managed hosting, monthly", D("3"), D("450.00")),
            Line("GX-SUP-HR", "Premium support (hours)", D("10"), D("95.00")),
        )),
        PurchaseOrder("PO-2024-1004", INITECH, date(2024, 9, 23), (
            Line("IN-CLN-MO", "Office cleaning service, monthly", D("1"), D("2400.00")),
            Line("IN-WIN-QT", "Window cleaning, quarterly visit", D("1"), D("680.00")),
            Line("IN-SUP-KT", "Washroom consumables kit", D("6"), D("42.50")),
        )),
    ]
    return {p.po_number: p for p in pos}


def build_invoices(pos: dict[str, PurchaseOrder]) -> list[Invoice]:
    D = Decimal

    def full(po: PurchaseOrder) -> tuple[Line, ...]:
        return po.lines

    po1, po2, po3, po4 = (pos[k] for k in ("PO-2024-1001", "PO-2024-1002", "PO-2024-1003", "PO-2024-1004"))
    return [
        Invoice("invoice_standard_digital.pdf", "ACME-INV-20417", ACME, date(2024, 9, 20), full(po1),
                po1.po_number, (("Sales tax", D("0.0700")),), notes="Net 30. Thank you for your business."),
        Invoice("invoice_multipage.pdf", "NWC-2024-09-0832", NORTHWIND, date(2024, 9, 27), full(po2),
                po2.po_number, (("Sales tax", D("0.0725")),), notes="Net 45. Deliveries completed in full on 24 Sep 2024."),
        Invoice("invoice_tax_separated.pdf", "GX-INV-5589", GLOBEX, date(2024, 10, 1), full(po3),
                po3.po_number, (("State sales tax", D("0.0625")), ("City service tax", D("0.0150")),
                                ("Technology levy", D("0.0050"))), notes="Services rendered 1-30 Sep 2024. Net 30."),
        # Same vendor as PO-1001 and similar goods, but no PO reference anywhere on the document.
        Invoice("invoice_missing_po.pdf", "ACME-INV-20433", ACME, date(2024, 10, 4),
                (Line("AC-HOS-12", "Hydraulic hose assembly, 12 in", D("6"), D("45.50")),
                 Line("AC-GLV-PR", "Safety gloves (pair)", D("50"), D("3.20"))),
                None, (("Sales tax", D("0.0700")),), notes="Net 30."),
        Invoice("invoice_scanned.pdf", "INI-77120", INITECH, date(2024, 10, 8), full(po4),
                po4.po_number, (("Sales tax", D("0.0650")),), scanned=True, notes="Net 30. Remit to address above."),
    ]


# --------------------------------------------------------------------------- rendering


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    return {
        "n": ParagraphStyle("n", parent=base, fontName="Helvetica", fontSize=9.5, leading=13),
        "b": ParagraphStyle("b", parent=base, fontName="Helvetica-Bold", fontSize=9.5, leading=13),
        "h": ParagraphStyle("h", parent=base, fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=colors.HexColor("#1f3a5f")),
        "v": ParagraphStyle("v", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=16),
        "s": ParagraphStyle("s", parent=base, fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.HexColor("#444444")),
    }


def render_invoice_pdf(inv: Invoice) -> bytes:
    """Render a digital (text-layer) invoice PDF to bytes."""
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title=f"Invoice {inv.invoice_number}", author=inv.vendor.name, invariant=1)
    meta = [
        ["Invoice No:", inv.invoice_number],
        ["Invoice Date:", inv.invoice_date.strftime("%d %b %Y")],
        ["Due Date:", (inv.invoice_date + timedelta(days=30)).strftime("%d %b %Y")],
    ]
    if inv.po_number:
        meta.append(["PO Number:", inv.po_number])
    meta.append(["Currency:", "USD"])
    meta_tbl = Table([[Paragraph(a, st["b"]), Paragraph(b, st["n"])] for a, b in meta], colWidths=[28 * mm, 45 * mm])
    head = Table([[
        [Paragraph(inv.vendor.name, st["v"])] + [Paragraph(a, st["n"]) for a in inv.vendor.address]
        + [Paragraph(f"Tax ID: {inv.vendor.tax_id}", st["s"])],
        [Paragraph("INVOICE", st["h"])],
    ]], colWidths=[100 * mm, 74 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    parties = Table([[
        [Paragraph("Bill To", st["b"])] + [Paragraph(x, st["n"]) for x in BILL_TO], meta_tbl,
    ]], colWidths=[100 * mm, 74 * mm])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    rows = [["#", "Item", "Description", "Qty", "Unit Price", "Amount"]]
    for i, ln in enumerate(inv.lines, 1):
        rows.append([str(i), ln.sku, Paragraph(ln.description, st["n"]), f"{ln.quantity:f}", money(ln.unit_price), money(ln.amount)])
    items = Table(rows, colWidths=[8 * mm, 24 * mm, 64 * mm, 14 * mm, 30 * mm, 34 * mm], repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f9")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.HexColor("#1f3a5f")),
    ]))

    tot = [["Subtotal", money(inv.subtotal)]]
    tot += [[f"{label} ({rate * 100:.2f}%)", money(amount)] for label, rate, amount in inv.tax_amounts]
    tot.append(["TOTAL DUE (USD)", money(inv.total)])
    totals = Table(tot, colWidths=[50 * mm, 34 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story = [head, Spacer(1, 8 * mm), parties, Spacer(1, 8 * mm), items, Spacer(1, 6 * mm), totals, Spacer(1, 8 * mm)]
    if inv.notes:
        story.append(Paragraph(inv.notes, st["s"]))
    story.append(Paragraph(f"Payment by bank transfer to {inv.vendor.name}. Quote the invoice number as reference.", st["s"]))

    def footer(c: canvas.Canvas, d: SimpleDocTemplate) -> None:
        c.saveState()
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawString(18 * mm, 10 * mm, f"{inv.vendor.name}  |  Invoice {inv.invoice_number}")
        c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {d.page}")
        c.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def render_scanned_pdf(digital_pdf: bytes, seed: int = 7, dpi: int = 200) -> bytes:
    """Simulate a scan: rasterise each page, add skew/blur/noise, and embed JPEGs in an image-only PDF."""
    rng = random.Random(seed)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4, invariant=1)
    with pymupdf.open(stream=digital_pdf, filetype="pdf") as src:
        for page in src:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), colorspace=pymupdf.csGRAY, alpha=False)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            img = img.rotate(rng.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=255)
            img = img.filter(ImageFilter.GaussianBlur(0.6))
            noise = Image.frombytes("L", img.size, rng.randbytes(img.width * img.height))  # seeded: reproducible
            img = Image.blend(img, noise, 0.06)
            jpg = io.BytesIO()
            img.save(jpg, format="JPEG", quality=72)
            jpg.seek(0)
            c.drawImage(ImageReader(jpg), 0, 0, width=A4[0], height=A4[1])
            c.showPage()
    c.save()
    return out.getvalue()


# --------------------------------------------------------------------------- orchestration


def generate_all(output_dir: Path = ROOT / "sample_data") -> tuple[list[GeneratedInvoice], Path]:
    """Write purchase_orders/purchase_orders.json and invoices/*.pdf under ``output_dir``."""
    pos = build_purchase_orders()
    po_dir, inv_dir = output_dir / "purchase_orders", output_dir / "invoices"
    po_dir.mkdir(parents=True, exist_ok=True)
    inv_dir.mkdir(parents=True, exist_ok=True)
    po_path = po_dir / "purchase_orders.json"
    po_path.write_text(json.dumps([p.to_json() for p in pos.values()], indent=2) + "\n", encoding="utf-8")

    generated: list[GeneratedInvoice] = []
    for inv in build_invoices(pos):
        digital = render_invoice_pdf(inv)
        pdf = render_scanned_pdf(digital) if inv.scanned else digital
        path = inv_dir / inv.file_name
        path.write_bytes(pdf)
        generated.append(GeneratedInvoice(
            path=path, invoice_number=inv.invoice_number, po_number=inv.po_number, vendor_name=inv.vendor.name,
            subtotal=inv.subtotal, total=inv.total, scanned=inv.scanned, tax_labels=[t[0] for t in inv.taxes],
        ))
    return generated, po_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "sample_data")
    args = parser.parse_args()
    generated, po_path = generate_all(args.output_dir)
    print(f"Wrote {po_path}")
    for g in generated:
        kind = "scanned" if g.scanned else "digital"
        print(f"Wrote {g.path}  [{kind}]  {g.invoice_number}  PO={g.po_number or '-'}  total={g.total}")


if __name__ == "__main__":
    main()
