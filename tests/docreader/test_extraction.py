"""End-to-end reader tests on generated fixtures. OCR is faked unless a test explicitly needs real Tesseract."""
from __future__ import annotations

import pytest

import app.services.docreader.pdf as pdf_module
from app.services.docreader import ExtractionError, ReaderConfig, extract_document, normalize_for_search
from app.services.docreader.ocr import OcrEngine
from tests.docreader import builders as b

CFG = ReaderConfig(ocr_languages="ara+eng")
needs_fitz = pytest.mark.skipif(pdf_module.fitz is None, reason="PyMuPDF not installed")


def _real_ocr(langs: str):
    engine = OcrEngine(ReaderConfig(ocr_languages=langs))
    return engine if engine.available and engine.languages == langs else None


# ------------------------------------------------------------------------------------------------------ PDFs
def test_english_text_pdf_uses_text_layer_without_ocr(tmp_path):
    path = b.make_text_pdf(tmp_path / "en.pdf", [b.EN_PAGE_1, b.EN_PAGE_2])
    ocr = b.FakeOcr()
    result = extract_document(path, "en.pdf", CFG, ocr)
    assert result.total_pages == 2 and result.extracted_pages == 2 and result.ocr_pages == 0
    assert ocr.calls == 0, "OCR must not run for normal text PDFs"
    assert [p.page for p in result.pages] == [1, 2]
    assert "Newton" in result.pages[0].text and "Photosynthesis" in result.pages[1].text
    assert result.primary_method in {"pymupdf", "pypdf"}


def test_arabic_text_pdf_is_readable_and_searchable(tmp_path):
    pytest.importorskip("arabic_reshaper")
    pytest.importorskip("bidi")
    path = b.make_arabic_pdf(tmp_path / "ar.pdf")
    result = extract_document(path, "ar.pdf", CFG, b.FakeOcr())
    page = result.pages[0]
    searchable = normalize_for_search(page.text)
    assert page.language in {"ar", "mixed"}
    assert "التعليم" in searchable or "التعليم"[::-1] in searchable  # logical or visual order
    assert page.text.strip()  # original text kept untouched


def test_scanned_pdf_pages_are_ocred_and_page_numbers_preserved(tmp_path):
    path = b.make_scanned_pdf(tmp_path / "scan.pdf", ["first scanned page", "second scanned page"])
    ocr = b.FakeOcr(["FIRST PAGE OCR TEXT", "SECOND PAGE OCR TEXT"])
    result = extract_document(path, "scan.pdf", CFG, ocr)
    assert ocr.calls == 2 and result.ocr_pages == 2
    assert [p.page for p in result.pages] == [1, 2]
    assert result.pages[0].text == "FIRST PAGE OCR TEXT" and result.pages[1].method == "ocr"
    assert result.pages[0].confidence == pytest.approx(0.9)


def test_scanned_arabic_pdf_with_fake_ocr_keeps_arabic_text(tmp_path):
    path = b.make_scanned_pdf(tmp_path / "scan_ar.pdf", ["صفحة ممسوحة ضوئيا"])
    result = extract_document(path, "scan_ar.pdf", CFG, b.FakeOcr([b.AR_LINES[0]]))
    assert result.pages[0].language == "ar" and result.pages[0].ocr_used


def test_mixed_pdf_ocrs_only_the_page_that_needs_it(tmp_path):
    path = b.make_mixed_pdf(tmp_path / "mixed.pdf", b.EN_PAGE_1, "scanned part")
    ocr = b.FakeOcr(["Recovered scanned words about the water cycle and rain."])
    result = extract_document(path, "mixed.pdf", CFG, ocr)
    assert ocr.calls == 1
    assert result.pages[0].ocr_used is False and result.pages[1].ocr_used is True


def test_ocr_text_is_merged_without_duplicating_existing_lines():
    merged = pdf_module.merge_texts("Line one about force\nLine two about mass",
                                    "Line one about force\nBrand new scanned line\nline two about mass")
    assert merged.count("Line one about force") == 1
    assert "Brand new scanned line" in merged
    assert merged.lower().count("line two about mass") == 1


def test_missing_ocr_reports_precise_error_instead_of_fake_success(tmp_path):
    path = b.make_scanned_pdf(tmp_path / "scan.pdf", ["only an image"])
    with pytest.raises(ExtractionError) as exc:
        extract_document(path, "scan.pdf", CFG, b.FakeOcr(available=False))
    assert exc.value.code == "OCR_REQUIRED_BUT_UNAVAILABLE"


def test_corrupted_pdf_is_rejected_with_a_clear_code(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a real pdf body \x00\x01\x02" * 4)
    with pytest.raises(ExtractionError) as exc:
        extract_document(path, "bad.pdf", CFG, b.FakeOcr())
    assert exc.value.code in {"PDF_MALFORMED", "PDF_NO_PAGES", "EMPTY_DOCUMENT", "CORRUPTED_DOCUMENT"}


def test_empty_pdf_is_not_reported_as_success(tmp_path):
    path = b.make_blank_pdf(tmp_path / "blank.pdf")
    with pytest.raises(ExtractionError) as exc:
        extract_document(path, "blank.pdf", CFG, b.FakeOcr([""]))
    assert exc.value.code == "EMPTY_DOCUMENT"


def test_pypdf_fallback_when_primary_engine_returns_nothing(tmp_path, monkeypatch):
    path = b.make_text_pdf(tmp_path / "en.pdf", [b.EN_PAGE_1])
    monkeypatch.setattr(pdf_module, "_pymupdf_page", lambda page, want_tables: ("", [], False, []))
    result = extract_document(path, "en.pdf", CFG, b.FakeOcr())
    assert result.pages[0].method == "pypdf" and "Newton" in result.pages[0].text
    assert result.pages[0].ocr_used is False


def test_works_with_pypdf_only_when_pymupdf_is_unavailable(tmp_path, monkeypatch):
    path = b.make_text_pdf(tmp_path / "en.pdf", [b.EN_PAGE_1])
    monkeypatch.setattr(pdf_module, "fitz", None)
    result = extract_document(path, "en.pdf", CFG, b.FakeOcr())
    assert result.pages[0].method == "pypdf" and result.extracted_pages == 1


@needs_fitz
def test_real_tesseract_english_ocr_on_scanned_pdf(tmp_path):
    ocr = _real_ocr("eng")
    if ocr is None:
        pytest.skip("Tesseract with English data not installed")
    path = b.make_scanned_pdf(tmp_path / "scan.pdf", ["Energy is never created or destroyed"])
    result = extract_document(path, "scan.pdf", ReaderConfig(ocr_languages="eng"), ocr)
    assert "energy" in result.pages[0].text.lower()


@needs_fitz
def test_real_tesseract_arabic_ocr_on_scanned_pdf(tmp_path):
    pytest.importorskip("arabic_reshaper")
    ocr = _real_ocr("ara+eng")
    if ocr is None:
        pytest.skip("Tesseract with Arabic+English data not installed")
    import arabic_reshaper
    from bidi.algorithm import get_display  # noqa: F401  (image text is drawn from shaped glyphs)

    path = b.make_scanned_pdf(tmp_path / "scan_ar.pdf", [arabic_reshaper.reshape("التعليم في الوحدة الأولى")])
    result = extract_document(path, "scan_ar.pdf", CFG, ocr)
    assert result.pages[0].language in {"ar", "mixed"}


# ------------------------------------------------------------------------------------------------------ other formats
def test_docx_with_headings_and_tables(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Chapter 1: Motion", level=1)
    d.add_paragraph("الحركة هي تغير موضع الجسم مع الزمن في الوحدة الأولى من الكتاب.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Speed", "Unit"
    table.cell(1, 0).text, table.cell(1, 1).text = "10", "m/s"
    path = tmp_path / "lesson.docx"
    d.save(path)
    result = extract_document(path, "lesson.docx", CFG, b.FakeOcr())
    kinds = {blk.kind for blk in result.pages[0].blocks}
    assert {"heading", "table"} <= kinds and "الحركة" in result.full_text and "| Speed | Unit |" in result.full_text


def test_xlsx_each_sheet_is_a_page(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Physics"
    wb.active.append(["Quantity", "Symbol"])
    wb.active.append(["Force", "F"])
    wb.create_sheet("الكيمياء").append(["العنصر", "الرمز"])
    wb["الكيمياء"].append(["الماء", "H2O"])
    path = tmp_path / "data.xlsx"
    wb.save(path)
    result = extract_document(path, "data.xlsx", CFG, b.FakeOcr())
    assert result.total_pages == 2 and [p.label for p in result.pages] == ["Physics", "الكيمياء"]
    assert "H2O" in result.full_text and result.pages[0].blocks[0].kind == "table"


def test_pptx_slides_become_pages(tmp_path):
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Gravity"
    slide.placeholders[1].text = "Objects attract each other with a force proportional to their masses."
    path = tmp_path / "deck.pptx"
    prs.save(path)
    result = extract_document(path, "deck.pptx", CFG, b.FakeOcr())
    assert result.pages[0].page == 1 and result.pages[0].label == "Gravity" and "attract" in result.full_text


def test_html_removes_scripts_and_keeps_structure(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<html><head><title>T</title><script>evil()</script></head><body><h1>Heading</h1>"
                    "<p>Paragraph text</p><table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
                    "</body></html>", encoding="utf-8")
    result = extract_document(path, "page.html", CFG, b.FakeOcr())
    assert "evil" not in result.full_text and "Paragraph text" in result.full_text and "| a | b |" in result.full_text


def test_txt_arabic_utf8_and_legacy_cp1256(tmp_path):
    utf8 = tmp_path / "a.txt"
    utf8.write_text("الدرس الأول في الفيزياء", encoding="utf-8")
    assert "الفيزياء" in extract_document(utf8, "a.txt", CFG, b.FakeOcr()).full_text
    legacy = tmp_path / "b.txt"
    legacy.write_bytes("الدرس الأول في الفيزياء".encode("cp1256"))
    assert "الفيزياء" in extract_document(legacy, "b.txt", CFG, b.FakeOcr()).full_text


def test_csv_and_json_and_markdown(tmp_path):
    (tmp_path / "t.csv").write_text("name,value\nforce,10\nmass,5\n", encoding="utf-8")
    (tmp_path / "t.json").write_text('{"topic": "physics", "level": 3}', encoding="utf-8")
    (tmp_path / "t.md").write_text("# Title\n\nSome *markdown* text.", encoding="utf-8")
    assert "| force | 10 |" in extract_document(tmp_path / "t.csv", "t.csv", CFG, b.FakeOcr()).full_text
    assert '"physics"' in extract_document(tmp_path / "t.json", "t.json", CFG, b.FakeOcr()).full_text
    assert extract_document(tmp_path / "t.md", "t.md", CFG, b.FakeOcr()).pages[0].blocks[0].kind == "heading"


def test_image_documents_are_ocred(tmp_path):
    path = b.make_scanned_image(tmp_path / "scan.png", "image text")
    result = extract_document(path, "scan.png", CFG, b.FakeOcr(["RECOGNIZED IMAGE TEXT"]))
    assert result.pages[0].method == "ocr" and result.ocr_pages == 1
