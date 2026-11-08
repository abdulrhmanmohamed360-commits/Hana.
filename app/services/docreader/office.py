"""Office readers: DOCX (python-docx + XML fallback), DOC (converter), XLSX (openpyxl), PPTX (python-pptx)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .arabic import clean_for_storage, detect_language
from .structure import analyze_text, table_to_markdown
from .textual import _table_pages, decode_bytes
from .types import (Block, CorruptedDocumentError, ExtractionError, PageResult, ReaderConfig, UnsupportedFileError)

log = logging.getLogger("eduforge.office")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


# ------------------------------------------------------------------------------------------------------------ DOCX
def read_docx(path: Path, config: ReaderConfig):
    try:
        return _read_docx_primary(path, config), {"engine": "python-docx"}
    except ExtractionError:
        raise
    except Exception as exc:
        log.info("docx_primary_failed", extra={"error": type(exc).__name__})
        return _read_docx_xml(path), {"engine": "xml-fallback", "fallback_reason": type(exc).__name__}


def _read_docx_primary(path: Path, config: ReaderConfig) -> list[PageResult]:
    import docx

    document = docx.Document(str(path))
    blocks: list[Block] = []
    lines: list[str] = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == f"{_W}p":
            from docx.text.paragraph import Paragraph

            para = Paragraph(child, document)
            math_nodes = child.findall(f".//{_M}oMath")
            text = para.text.strip()
            style = (para.style.name if para.style is not None else "") or ""
            if math_nodes:
                for node in math_nodes:
                    eq = "".join(node.itertext()).strip()
                    if eq:
                        blocks.append(Block("equation", eq, None, 0, 0.7, {"source": "omml", "raw_preserved": True}))
                        lines.append(eq)
            if not text:
                continue
            m = re.match(r"(?:Heading|عنوان)\s*(\d)", style, re.IGNORECASE)
            if style.lower() == "title":
                blocks.append(Block("heading", text, None, 1, 0.95, {"chapter": True}))
            elif m:
                lvl = int(m.group(1))
                blocks.append(Block("heading", text, None, lvl, 0.95, {"chapter": lvl == 1}))
            elif "list" in style.lower():
                blocks.append(Block("list", text, None, 0, 0.9))
            else:
                blocks.extend(analyze_text(text, None))
            lines.append(text)
        elif tag == f"{_W}tbl":
            from docx.table import Table

            table = Table(child, document)
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            md = table_to_markdown(rows)
            if md:
                blocks.append(Block("table", md, None, 0, 0.9, {"rows": len(rows), "engine": "python-docx"}))
                lines.append(md)
    text = clean_for_storage("\n".join(lines))
    if not text:
        return []
    return [PageResult(None, text, "native", 0.95, detect_language(text), blocks=blocks)]


def _read_docx_xml(path: Path) -> list[PageResult]:
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise CorruptedDocumentError("DOCX could not be read by any engine", code="DOCX_MALFORMED") from exc
    paragraphs = []
    for p in root.iter(f"{_W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()
        if text:
            paragraphs.append(text)
    text = clean_for_storage("\n".join(paragraphs))
    if not text:
        return []
    return [PageResult(None, text, "native", 0.8, detect_language(text), blocks=analyze_text(text, None, base_confidence=0.8),
                       issues=["xml_fallback_used"])]


# ------------------------------------------------------------------------------------------------------------ DOC
def read_doc(path: Path, config: ReaderConfig):
    """Legacy Word: LibreOffice (best Arabic fidelity) -> antiword -> catdoc. All run with timeouts, no shell."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        with tempfile.TemporaryDirectory(prefix="eduforge-doc-") as tmp:
            try:
                subprocess.run([soffice, "--headless", "--norestore", "--convert-to", "docx", "--outdir", tmp, str(path)],
                               check=True, timeout=120, capture_output=True,
                               env={"HOME": tmp, "PATH": "/usr/bin:/bin:/usr/local/bin"})
                out = next(Path(tmp).glob("*.docx"), None)
                if out is not None:
                    pages = _read_docx_primary(out, config)
                    return pages, {"engine": "libreoffice+python-docx"}
            except (subprocess.SubprocessError, OSError, ExtractionError) as exc:
                log.info("soffice_failed", extra={"error": type(exc).__name__})
    for tool, args in (("antiword", ["-m", "UTF-8.txt"]), ("catdoc", ["-d", "utf-8"])):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            proc = subprocess.run([exe, *args, str(path)], check=True, timeout=60, capture_output=True)
        except (subprocess.SubprocessError, OSError):
            continue
        text, _ = decode_bytes(proc.stdout)
        text = clean_for_storage(text)
        if text:
            page = PageResult(None, text, "native", 0.7, detect_language(text), blocks=analyze_text(text, None, base_confidence=0.7),
                              issues=[f"legacy_doc_via_{tool}"])
            return [page], {"engine": tool}
    raise UnsupportedFileError("No converter is available for legacy .doc files (install libreoffice, antiword or catdoc)",
                               code="DOC_CONVERTER_MISSING")


# ------------------------------------------------------------------------------------------------------------ XLSX
def read_xlsx(path: Path, config: ReaderConfig):
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise CorruptedDocumentError("XLSX could not be opened", code="XLSX_MALFORMED",
                                     details={"error": type(exc).__name__}) from exc
    pages: list[PageResult] = []
    try:
        for index, sheet in enumerate(workbook.worksheets, start=1):
            rows: list[list[str]] = []
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= config.max_sheet_rows:
                    break
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if not rows:
                continue
            for page in _table_pages(rows, config, label=sheet.title, page_numbers=True, first_number=index):
                pages.append(page)
    finally:
        workbook.close()
    return pages, {"sheets": len(workbook.sheetnames) if hasattr(workbook, "sheetnames") else None}


# ------------------------------------------------------------------------------------------------------------ PPTX
def _shape_texts(shape, out: list[str], tables: list[Block]) -> None:
    if getattr(shape, "shape_type", None) == 6 and hasattr(shape, "shapes"):  # group
        for sub in shape.shapes:
            _shape_texts(sub, out, tables)
        return
    if getattr(shape, "has_table", False) and shape.has_table:
        rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
        md = table_to_markdown(rows)
        if md:
            tables.append(Block("table", md, None, 0, 0.9, {"rows": len(rows), "engine": "python-pptx"}))
            out.append(md)
        return
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        text = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip()).strip()
        if text:
            out.append(text)


def read_pptx(path: Path, config: ReaderConfig):
    try:
        from pptx import Presentation

        deck = Presentation(str(path))
    except Exception as exc:
        raise CorruptedDocumentError("PPTX could not be opened", code="PPTX_MALFORMED",
                                     details={"error": type(exc).__name__}) from exc
    pages: list[PageResult] = []
    for number, slide in enumerate(deck.slides, start=1):
        if number > config.max_pages:
            break
        parts: list[str] = []
        tables: list[Block] = []
        title = None
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip() or None
        for shape in slide.shapes:
            _shape_texts(shape, parts, tables)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(notes)
        text = clean_for_storage("\n".join(parts))
        if not text:
            continue
        blocks = []
        if title:
            blocks.append(Block("heading", title, number, 1, 0.95, {"chapter": False, "slide_title": True}))
        blocks.extend(analyze_text(text, number))
        for t in tables:
            t.page = number
        pages.append(PageResult(number, text, "native", 0.95, detect_language(text), blocks=blocks + tables, label=title))
    return pages, {"slides": len(deck.slides)}
