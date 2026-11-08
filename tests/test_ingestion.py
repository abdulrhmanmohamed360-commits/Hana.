from pathlib import Path

import pytest

from app.services.ingestion import IngestionError, content_hash, extract_text, normalize_text, store_upload


def test_normalize_text_and_hash_are_stable():
    a = normalize_text("  Alpha\r\n\r\n\r\n beta  ")
    b = normalize_text("Alpha\n\nbeta")
    assert a == b == "Alpha\n\nbeta"
    assert content_hash(a) == content_hash(b)


def test_text_extraction(tmp_path):
    p = tmp_path / "lesson.txt"
    p.write_text("  First line\r\nSecond   line  ", encoding="utf-8")
    text, mime = extract_text(p)
    assert text == "First line\nSecond line"
    assert mime == "text/plain"


def test_html_extraction_removes_scripts(tmp_path):
    p = tmp_path / "lesson.html"
    p.write_text("<html><script>secret()</script><body><h1>Title</h1><p>Text</p></body></html>")
    text, mime = extract_text(p)
    assert "Title" in text and "Text" in text and "secret" not in text
    assert mime == "text/html"


def test_json_extraction(tmp_path):
    p = tmp_path / "lesson.json"
    p.write_text('{"b": 2, "a": "hello"}')
    text, mime = extract_text(p)
    assert '"hello"' in text and mime == "application/json"


def test_empty_document_rejected(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text(" \n ")
    with pytest.raises(IngestionError):
        extract_text(p)


def test_upload_rejects_unsupported_and_too_large(tmp_path):
    with pytest.raises(IngestionError):
        store_upload(b"x", "file.exe", str(tmp_path), 100)
    with pytest.raises(IngestionError):
        store_upload(b"x" * 101, "file.txt", str(tmp_path), 100)


def test_upload_uses_generated_filename(tmp_path):
    path, digest = store_upload(b"hello", "../../unsafe.txt", str(tmp_path), 100)
    assert Path(path).parent == tmp_path.resolve()
    assert Path(path).name.endswith(".txt")
    assert len(digest) == 64


def test_chunk_segments_preserve_pdf_page_ranges():
    from app.services.chunking import TextSegment, chunk_segments

    chunks = chunk_segments([
        TextSegment("A" * 500, 1),
        TextSegment("B" * 500, 2),
    ], max_chars=500, overlap_chars=50)
    assert chunks
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2
    assert all(len(chunk.content) <= 500 for chunk in chunks)
    assert all(len(chunk.content_hash) == 64 for chunk in chunks)
