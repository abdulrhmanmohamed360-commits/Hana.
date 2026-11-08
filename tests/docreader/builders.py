"""Fixture builders for the document-reader tests. Everything is generated at test time (no binary fixtures)."""
from __future__ import annotations

import io
from pathlib import Path

from app.services.docreader.ocr import OcrOutput

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
EN_PAGE_1 = "Newton's second law states that force equals mass times acceleration for every object."
EN_PAGE_2 = "Photosynthesis converts light energy into chemical energy stored in glucose molecules."
AR_LINES = [
    "هذا هو الدرس الأول في الوحدة التي تشرح التعليم",
    "من المهم أن نعرف ما هو العلم وكيف يفيد الإنسان على الأرض",
    "في هذه الوحدة نتعلم الحركة والقوة والطاقة",
]


class FakeOcr:
    """Deterministic stand-in for OcrEngine (same interface the readers use)."""

    def __init__(self, texts=("OCR RECOGNIZED TEXT",), confidence=0.9, available=True):
        self.available, self.languages, self.issues = available, "ara+eng", []
        self.texts, self.confidence, self.calls = list(texts), confidence, 0

    def recognize_image(self, image, *, timeout=None):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return OcrOutput(text, self.confidence, self.languages, len(text.split()), [])


def _canvas(path: Path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    return canvas.Canvas(str(path), pagesize=A4), A4


def make_text_pdf(path: Path, pages: list[str]) -> Path:
    c, (w, h) = _canvas(path)
    for text in pages:
        c.setFont("Helvetica", 12)
        y = h - 80
        for line in text.split("\n"):
            c.drawString(60, y, line)
            y -= 18
        c.showPage()
    c.save()
    return path


def make_arabic_pdf(path: Path, lines: list[str] = AR_LINES) -> Path:
    import arabic_reshaper
    from bidi.algorithm import get_display
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("DejaVu", DEJAVU))
    c, (w, h) = _canvas(path)
    c.setFont("DejaVu", 14)
    y = h - 80
    for line in lines:
        c.drawRightString(w - 60, y, get_display(arabic_reshaper.reshape(line)))
        y -= 24
    c.showPage()
    c.save()
    return path


def _text_image(text: str):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1240, 600), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(DEJAVU, 44)
    y = 60
    for line in text.split("\n"):
        draw.text((60, y), line, fill=0, font=font)
        y += 70
    return img


def make_scanned_pdf(path: Path, texts: list[str]) -> Path:
    """Pages that are only raster images (no text layer)."""
    from reportlab.lib.utils import ImageReader

    c, (w, h) = _canvas(path)
    for text in texts:
        buf = io.BytesIO()
        _text_image(text).save(buf, format="PNG")
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, h - 300, width=w, height=300)
        c.showPage()
    c.save()
    return path


def make_mixed_pdf(path: Path, text_page: str, scanned_text: str) -> Path:
    """Page 1 has a text layer, page 2 is a scan."""
    from reportlab.lib.utils import ImageReader

    c, (w, h) = _canvas(path)
    c.setFont("Helvetica", 12)
    c.drawString(60, h - 80, text_page)
    c.showPage()
    buf = io.BytesIO()
    _text_image(scanned_text).save(buf, format="PNG")
    buf.seek(0)
    c.drawImage(ImageReader(buf), 0, h - 300, width=w, height=300)
    c.showPage()
    c.save()
    return path


def make_blank_pdf(path: Path) -> Path:
    c, _ = _canvas(path)
    c.showPage()
    c.save()
    return path


def make_scanned_image(path: Path, text: str, fmt: str = "PNG") -> Path:
    _text_image(text).save(path, format=fmt)
    return path
