import io
import zipfile

import pytest

from app.services.docreader import ExtractionError, ReaderConfig, extract_document, safe_filename
from app.services.docreader.security import detect_file_type
from tests.docreader.builders import FakeOcr

CFG = ReaderConfig()


def test_safe_filename_strips_paths_and_keeps_arabic():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\x\\كتاب الفيزياء.PDF") == "كتاب الفيزياء.pdf"
    assert safe_filename("") == "document"
    assert "/" not in safe_filename("a/b\x00c.txt")


def test_signature_must_match_extension(tmp_path):
    fake_pdf = tmp_path / "x.pdf"
    fake_pdf.write_text("just text", encoding="utf-8")
    with pytest.raises(ExtractionError) as exc:
        detect_file_type(fake_pdf, "x.pdf")
    assert exc.value.code == "SIGNATURE_MISMATCH"
    exe_as_txt = tmp_path / "x.txt"
    exe_as_txt.write_bytes(b"MZ\x90\x00" + b"\x00" * 100)
    with pytest.raises(ExtractionError):
        detect_file_type(exe_as_txt, "x.txt")


def test_unsupported_extension_and_empty_and_oversize(tmp_path):
    f = tmp_path / "a.exe"
    f.write_bytes(b"abc")
    with pytest.raises(ExtractionError) as exc:
        detect_file_type(f, "a.exe")
    assert exc.value.code == "UNSUPPORTED_FILE"
    empty = tmp_path / "e.txt"
    empty.write_bytes(b"")
    with pytest.raises(ExtractionError) as exc:
        detect_file_type(empty, "e.txt")
    assert exc.value.code == "FILE_EMPTY"
    big = tmp_path / "b.txt"
    big.write_bytes(b"x" * 200)
    with pytest.raises(ExtractionError) as exc:
        detect_file_type(big, "b.txt", ReaderConfig(max_file_bytes=100))
    assert exc.value.code == "FILE_TOO_LARGE"


def test_zip_bomb_and_zip_slip_are_rejected(tmp_path):
    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w/>")
        z.writestr("word/huge.bin", b"\x00" * (30 * 1024 * 1024))
    with pytest.raises(ExtractionError) as exc:
        extract_document(bomb, "bomb.docx", ReaderConfig(office_max_uncompressed_bytes=10 * 1024 * 1024), FakeOcr())
    assert exc.value.code in {"ARCHIVE_TOO_LARGE", "ARCHIVE_BOMB"}
    slip = tmp_path / "slip.docx"
    with zipfile.ZipFile(slip, "w") as z:
        z.writestr("word/document.xml", "<w/>")
        z.writestr("../evil.txt", "x")
    with pytest.raises(ExtractionError) as exc:
        extract_document(slip, "slip.docx", CFG, FakeOcr())
    assert exc.value.code == "ARCHIVE_UNSAFE_PATH"


def test_zip_that_is_not_an_office_document_is_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hello")
    path = tmp_path / "fake.docx"
    path.write_bytes(buf.getvalue())
    with pytest.raises(ExtractionError) as exc:
        detect_file_type(path, "fake.docx")
    assert exc.value.code == "SIGNATURE_MISMATCH"


def test_declared_mime_contradiction_is_rejected(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello world", encoding="utf-8")
    with pytest.raises(ExtractionError) as exc:
        extract_document(path, "a.txt", CFG, FakeOcr(), declared_mime="application/pdf")
    assert exc.value.code == "MIME_MISMATCH"
    extract_document(path, "a.txt", CFG, FakeOcr(), declared_mime="application/octet-stream")  # generic is fine
