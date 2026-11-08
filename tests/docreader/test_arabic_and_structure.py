from app.services.docreader.arabic import (assess_arabic, best_arabic_variant, clean_for_storage, detect_language,
                                           normalize_for_search, reverse_visual_order)
from app.services.docreader.structure import analyze_text, classify_line
from app.services.docreader.types import ExtractionResult, PageResult
from app.services.structured_chunking import chunk_result

FORWARD = "هذا هو الدرس الأول من الوحدة التي تشرح ما هو العلم في التعليم على الاطلاق"


def test_search_normalization_folds_ligatures_diacritics_and_digits_without_touching_original():
    original = "\ufefb السَّلام ١٢٣ أحمد؟"
    searchable = normalize_for_search(original)
    assert "لا" in searchable and "َ" not in searchable and "123" in searchable and "احمد" in searchable
    assert original == "\ufefb السَّلام ١٢٣ أحمد؟"  # strings are immutable; callers keep the source text


def test_storage_cleanup_preserves_arabic_marks_and_rtl_controls():
    text = "السَّلام\u200f عليكم"
    assert clean_for_storage(text) == text
    assert "\x00" not in clean_for_storage("a\x00b")


def test_language_detection_and_mixed_text():
    assert detect_language("مرحبا بكم في الدرس") == "ar"
    assert detect_language("Hello students") == "en"
    assert detect_language("درس الفيزياء Physics lesson الحركة motion") == "mixed"


def test_reversed_arabic_is_detected_and_repaired():
    reversed_text = FORWARD[::-1]
    quality = assess_arabic(reversed_text)
    assert quality.likely_reversed and "reversed_order" in quality.issues
    repaired, new_quality, note = best_arabic_variant(reversed_text)
    assert note == "reversed_visual_order_repaired" and repaired == FORWARD and not new_quality.likely_reversed
    assert reverse_visual_order(reversed_text) == FORWARD


def test_healthy_arabic_is_not_modified():
    text, quality, note = best_arabic_variant(FORWARD)
    assert text == FORWARD and note is None and quality.score == 1.0


def test_unmapped_glyphs_are_flagged_as_broken():
    garbage = "هذا الدرس " + "\ue001" * 15 + " (cid:12)(cid:13)(cid:14) في الوحدة الاولى من الكتاب"
    q = assess_arabic(garbage)
    assert "unmapped_glyphs" in q.issues and q.score < 1.0


def test_structure_headings_questions_equations_chemistry_captions():
    assert classify_line("Chapter 1: Motion")[0] == "heading"
    assert classify_line("الوحدة الأولى: الحركة")[0] == "heading"
    assert classify_line("1. What is velocity?")[0] == "question"
    assert classify_line("F = m × a")[0] == "equation"
    kind, _, _, meta = classify_line("2H₂ + O₂ → 2H₂O")
    assert kind == "equation" and meta.get("chemical")
    assert classify_line("Figure 2.1: Free fall")[0] == "caption"
    assert classify_line("Example 3: Solve for x")[0] == "example"


def _result(pages):
    return ExtractionResult("pdf", "application/pdf", pages, len(pages))


def _page(number, text, method="pymupdf", confidence=0.9):
    return PageResult(number, text, method, confidence, "en", blocks=analyze_text(text, number, base_confidence=confidence))


def test_chunks_keep_page_ranges_chapters_and_provenance():
    p1 = _page(1, "Chapter 1: Motion\n\n" + "Velocity describes how fast position changes over time. " * 12)
    p2 = _page(2, "1.2 Acceleration\n\n" + "Acceleration is the rate of change of velocity. " * 12, "pymupdf+ocr", 0.7)
    p3 = _page(3, "Chapter 2: Energy\n\n" + "Energy is conserved in isolated systems. " * 10)
    chunks = chunk_result(_result([p1, p2, p3]), max_chars=500, overlap_chars=60)
    assert chunks and all(len(c.content) <= 500 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chapter_title == "Chapter 1: Motion" and chunks[0].page_start == 1
    assert any(c.chapter_title == "Chapter 2: Energy" and c.page_start == 3 for c in chunks)
    assert any("ocr" in c.extraction_method and c.extraction_confidence < 0.9 for c in chunks)
    assert all(len(c.content_hash) == 64 for c in chunks)
    assert len({c.content_hash for c in chunks}) == len(chunks)
    # never leak a chapter into the next one
    for c in chunks:
        assert not ("Chapter 2" in (c.chapter_title or "") and "Velocity describes" in c.content)


def test_chunking_is_deterministic():
    pages = [_page(1, "Chapter 1: A\n\n" + "Some sentence about physics. " * 80)]
    a = chunk_result(_result(pages), max_chars=400, overlap_chars=40)
    b = chunk_result(_result(pages), max_chars=400, overlap_chars=40)
    assert [(c.content, c.content_hash) for c in a] == [(c.content, c.content_hash) for c in b]


def test_tables_are_kept_whole_and_split_with_header():
    rows = "\n".join(f"| row{i} | value{i} |" for i in range(80))
    table = "| name | value |\n| --- | --- |\n" + rows
    page = PageResult(1, table, "pymupdf", 0.9, "en", blocks=analyze_text("intro paragraph text here", 1))
    from app.services.docreader.types import Block

    page.blocks.append(Block("table", table, 1, 0, 0.8))
    chunks = chunk_result(_result([page]), max_chars=500, overlap_chars=0)
    table_chunks = [c for c in chunks if c.kind == "table"]
    assert len(table_chunks) > 1 and all(c.content.startswith("| name | value |") for c in table_chunks)
