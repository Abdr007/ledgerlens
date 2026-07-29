"""Generate real invoice documents for seeding, evaluation and tests.

Every artefact this module produces is a **real file**: a genuine PDF with a real
text layer, or a genuinely degraded raster image. Nothing downstream is stubbed —
the same bytes go through the same hashing, routing, extraction, validation and
screening path a customer upload would.

The scan generator does not merely rasterise. It rotates, blurs, adds sensor
noise, shifts the white point and re-encodes as lossy JPEG, so the vision lane is
exercised against something that actually looks like a phone photo of a printout
rather than a clean render.

Ground truth is computed from the spec, not read back from the document, so the
evaluation harness is scoring extraction rather than grading its own homework.
"""

from __future__ import annotations

import io
import secrets
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from PIL import Image, ImageEnhance, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

# Pillow's own decompression-bomb ceiling. Our render targets are ~2 MP; a
# hostile or malformed source should hit this long before it exhausts memory.
Image.MAX_IMAGE_PIXELS = 64_000_000

_PAGE_WIDTH, _PAGE_HEIGHT = A4
_MARGIN: Final = 18 * mm
_CENT: Final = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class LineSpec:
    description: str
    qty: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return _money(self.qty * self.unit_price)


@dataclass(frozen=True, slots=True)
class InvoiceSpec:
    """A document to render, plus the ground truth it should extract to."""

    vendor: str
    vendor_address: str
    trn: str
    invoice_number: str
    issue_date: date
    due_date: date
    bill_to: str
    lines: tuple[LineSpec, ...]
    currency: str = "AED"
    vat_rate: Decimal = Decimal("0.05")
    payment_terms: str = "Net 30"
    # Deliberate defects, used to build documents that must fail validation.
    forced_subtotal: Decimal | None = None
    forced_tax: Decimal | None = None
    forced_total: Decimal | None = None
    note: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def subtotal(self) -> Decimal:
        if self.forced_subtotal is not None:
            return _money(self.forced_subtotal)
        return _money(sum((line.amount for line in self.lines), start=Decimal("0")))

    @property
    def tax(self) -> Decimal:
        if self.forced_tax is not None:
            return _money(self.forced_tax)
        return _money(self.subtotal * self.vat_rate)

    @property
    def total(self) -> Decimal:
        if self.forced_total is not None:
            return _money(self.forced_total)
        return _money(self.subtotal + self.tax)

    def ground_truth(self) -> dict[str, Any]:
        """The labels the evaluation harness scores against."""
        return {
            "vendor": self.vendor,
            "invoice_number": self.invoice_number,
            "issue_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "subtotal": str(self.subtotal),
            "tax": str(self.tax),
            "total": str(self.total),
            "currency": self.currency,
            "payment_terms": self.payment_terms,
            "line_items": [
                {
                    "description": line.description,
                    "qty": str(line.qty),
                    "unit_price": str(line.unit_price),
                    "amount": str(line.amount),
                }
                for line in self.lines
            ],
        }


def _fmt(value: Decimal) -> str:
    return f"{value:,.2f}"


def render_invoice_pdf(spec: InvoiceSpec) -> bytes:
    """Render a spec to a real, text-layer PDF."""
    buffer = io.BytesIO()
    canvas = pdf_canvas.Canvas(buffer, pagesize=A4)
    canvas.setTitle(f"Invoice {spec.invoice_number}")
    canvas.setAuthor(spec.vendor)

    y = _PAGE_HEIGHT - _MARGIN

    # -- Issuer block
    canvas.setFont("Helvetica-Bold", 15)
    canvas.drawString(_MARGIN, y, spec.vendor)
    y -= 6 * mm
    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(colors.HexColor("#444444"))
    for address_line in spec.vendor_address.split("\n"):
        canvas.drawString(_MARGIN, y, address_line)
        y -= 4.4 * mm
    canvas.drawString(_MARGIN, y, f"TRN: {spec.trn}")
    canvas.setFillColor(colors.black)

    # -- Document title
    canvas.setFont("Helvetica-Bold", 19)
    canvas.drawRightString(_PAGE_WIDTH - _MARGIN, _PAGE_HEIGHT - _MARGIN - 2 * mm, "TAX INVOICE")

    y -= 12 * mm
    canvas.setLineWidth(0.6)
    canvas.setStrokeColor(colors.HexColor("#B9C2CE"))
    canvas.line(_MARGIN, y, _PAGE_WIDTH - _MARGIN, y)
    y -= 8 * mm

    # -- Reference block. Labels are explicit so a rule-based reader can find them.
    canvas.setFont("Helvetica", 9.5)
    right = _PAGE_WIDTH / 2 + 6 * mm
    canvas.drawString(_MARGIN, y, f"Invoice No: {spec.invoice_number}")
    canvas.drawString(right, y, f"Invoice Date: {spec.issue_date.isoformat()}")
    y -= 5.4 * mm
    canvas.drawString(_MARGIN, y, f"Payment Terms: {spec.payment_terms}")
    canvas.drawString(right, y, f"Due Date: {spec.due_date.isoformat()}")
    y -= 10 * mm

    canvas.setFont("Helvetica-Bold", 9.5)
    canvas.drawString(_MARGIN, y, "Bill To:")
    y -= 5 * mm
    canvas.setFont("Helvetica", 9.5)
    for recipient_line in spec.bill_to.split("\n"):
        canvas.drawString(_MARGIN, y, recipient_line)
        y -= 4.8 * mm
    y -= 6 * mm

    # -- Item table
    col_qty = _PAGE_WIDTH - _MARGIN - 78 * mm
    col_price = _PAGE_WIDTH - _MARGIN - 46 * mm
    col_amount = _PAGE_WIDTH - _MARGIN

    canvas.setFillColor(colors.HexColor("#EEF2F7"))
    canvas.rect(
        _MARGIN - 2 * mm, y - 2 * mm, _PAGE_WIDTH - 2 * _MARGIN + 4 * mm, 7 * mm, stroke=0, fill=1
    )
    canvas.setFillColor(colors.black)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(_MARGIN, y, "Description")
    canvas.drawRightString(col_qty, y, "Qty")
    canvas.drawRightString(col_price, y, "Unit Price")
    canvas.drawRightString(col_amount, y, "Amount")
    y -= 7.5 * mm

    canvas.setFont("Helvetica", 9)
    for item in spec.lines:
        canvas.drawString(_MARGIN, y, item.description[:58])
        canvas.drawRightString(col_qty, y, f"{item.qty.normalize():f}")
        canvas.drawRightString(col_price, y, _fmt(item.unit_price))
        canvas.drawRightString(col_amount, y, _fmt(item.amount))
        y -= 5.6 * mm

    y -= 3 * mm
    canvas.setStrokeColor(colors.HexColor("#B9C2CE"))
    canvas.line(col_qty - 26 * mm, y, _PAGE_WIDTH - _MARGIN, y)
    y -= 7 * mm

    # -- Totals
    label_x = col_price
    canvas.setFont("Helvetica", 9.5)
    canvas.drawRightString(label_x, y, "Subtotal")
    canvas.drawRightString(col_amount, y, _fmt(spec.subtotal))
    y -= 5.6 * mm
    vat_label = f"VAT {spec.vat_rate * 100:.0f}%" if spec.vat_rate else "VAT"
    canvas.drawRightString(label_x, y, vat_label)
    canvas.drawRightString(col_amount, y, _fmt(spec.tax))
    y -= 7 * mm
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawRightString(label_x, y, "Total Due")
    canvas.drawRightString(col_amount, y, f"{spec.currency} {_fmt(spec.total)}")

    # -- Footer
    y -= 16 * mm
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#555555"))
    canvas.drawString(_MARGIN, y, "Bank: Emirates NBD   IBAN: AE070331234567890123456")
    if spec.note:
        y -= 4.4 * mm
        canvas.drawString(_MARGIN, y, spec.note)
    canvas.drawString(_MARGIN, _MARGIN, "Generated by LedgerLens document fixtures.")

    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _jitter(scale: float) -> float:
    """Deterministically-seeded-free small perturbation in [-scale, +scale]."""
    return (secrets.randbelow(2_001) / 1_000.0 - 1.0) * scale


def degrade_to_scan(
    pdf_bytes: bytes, *, dpi: int = 150, quality: int = 72, seed_free: bool = True
) -> bytes:
    """Turn a clean PDF into something that looks photographed, not exported.

    Applied in the order a real capture would: render -> optical blur ->
    perspective-ish rotation -> sensor noise -> exposure shift -> lossy encode.
    """
    import pymupdf

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        page = document[0]
        zoom = dpi / 72.0
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")

    wobble = _jitter(1.2) if seed_free else 0.9
    exposure = 1.0 + (_jitter(0.08) if seed_free else 0.04)
    contrast = 1.0 - (abs(_jitter(0.10)) if seed_free else 0.05)

    image = image.filter(ImageFilter.GaussianBlur(radius=0.6))
    image = image.rotate(
        wobble, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(252, 251, 248)
    )
    image = ImageEnhance.Brightness(image).enhance(exposure)
    image = ImageEnhance.Contrast(image).enhance(contrast)

    # Sensor noise: a low-amplitude per-pixel perturbation across the frame.
    noise = Image.effect_noise(image.size, 12).convert("L")
    image = Image.blend(image, Image.merge("RGB", (noise, noise, noise)), alpha=0.045)

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()
