"""Plain-text family readers: TXT, Markdown, HTML, JSON, CSV."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .arabic import clean_for_storage, detect_language
from .structure import analyze_text, table_to_markdown
from .types import Block, CorruptedDocumentError, PageResult, ReaderConfig


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode with BOM detection, then UTF-8, then Windows-1256 (common legacy Arabic), then Latin-1."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp1256")
        if sum(1 for ch in text if "\u0600" <= ch <= "\u06ff") > 0:
            return text, "cp1256"
    except UnicodeDecodeError:
        pass
    return data.decode("latin-1", errors="replace"), "latin-1"


def _read_text(path: Path, config: ReaderConfig) -> tuple[str, str]:
    data = path.read_bytes()
    if len(data) > config.max_file_bytes:
        raise CorruptedDocumentError("Text file too large", code="FILE_TOO_LARGE")
    return decode_bytes(data)


def _single(text: str, kind: str, mime: str, encoding: str, base: float = 1.0) -> tuple[list[PageResult], dict]:
    stored = clean_for_storage(text)
    if not stored:
        return [], {"encoding": encoding}
    page = PageResult(None, stored, "native", base, detect_language(stored), blocks=analyze_text(stored, None, base_confidence=base))
    return [page], {"encoding": encoding}


def read_txt(path: Path, config: ReaderConfig, kind: str = "txt"):
    text, enc = _read_text(path, config)
    mime = "text/markdown" if kind == "md" else "text/plain"
    pages, meta = _single(text, kind, mime, enc)
    return pages, mime, meta


def read_html(path: Path, config: ReaderConfig):
    raw, enc = _read_text(path, config)
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:  # lxml missing/unusable -> stdlib parser fallback
        soup = BeautifulSoup(raw, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template", "iframe", "object", "embed"]):
        node.decompose()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "") or None
    blocks: list[Block] = []
    for table in soup.find_all("table"):
        rows = [[cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
        md = table_to_markdown(rows)
        if md:
            blocks.append(Block("table", md, None, 0, 0.9, {"rows": len(rows), "engine": "html"}))
            table.replace_with(soup.new_string("\n" + md + "\n"))
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.insert_before(soup.new_string("\n" + "#" * level + " "))
            h.insert_after(soup.new_string("\n"))
    for tag in soup.find_all(["p", "div", "li", "br", "tr", "section", "article"]):
        tag.insert_after(soup.new_string("\n"))
    text = soup.get_text("")
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    pages, meta = _single(text, "html", "text/html", enc)
    meta["title"] = title
    return pages, "text/html", meta


def read_json(path: Path, config: ReaderConfig):
    raw, enc = _read_text(path, config)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CorruptedDocumentError("Invalid JSON document", code="JSON_INVALID") from exc
    text = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    pages, meta = _single(text, "json", "application/json", enc)
    return pages, "application/json", meta


def read_csv(path: Path, config: ReaderConfig):
    raw, enc = _read_text(path, config)
    sample = raw[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: list[list[str]] = []
    for i, row in enumerate(csv.reader(io.StringIO(raw), dialect)):
        if i >= config.max_sheet_rows:
            break
        rows.append(row)
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], "text/csv", {"encoding": enc}
    return _table_pages(rows, config, label=None, page_numbers=False), "text/csv", {"encoding": enc, "rows": len(rows)}


def _table_pages(rows: list[list], config: ReaderConfig, *, label: str | None, page_numbers: bool,
                 first_number: int | None = None) -> list[PageResult]:
    """Split a big table into groups that keep the header row, one PageResult per group."""
    width = max(len(r) for r in rows) or 1
    per_group = max(5, config.max_table_cells_per_chunk // width)
    header, body = rows[0], rows[1:]
    groups = [body[i:i + per_group] for i in range(0, len(body), per_group)] or [[]]
    pages: list[PageResult] = []
    for gi, group in enumerate(groups):
        md = table_to_markdown([header] + group)
        if not md:
            continue
        text = (f"{label}\n" if label else "") + md
        start_row = 2 + gi * per_group
        block = Block("table", md, first_number, 0, 0.95,
                      {"rows": len(group) + 1, "cols": width, "row_start": start_row,
                       "row_end": start_row + len(group) - 1, "sheet": label})
        pages.append(PageResult(first_number, text, "native", 0.95, detect_language(text), blocks=[block], label=label))
    return pages
