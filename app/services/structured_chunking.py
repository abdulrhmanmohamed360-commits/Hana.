"""Structure-aware chunking over docreader results. Keeps page ranges, section/chapter context and extraction
provenance on every chunk. Deterministic: the same input always yields the same chunks and hashes."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field

from app.services.docreader.arabic import detect_language
from app.services.docreader.types import Block, ExtractionResult, PageResult

_SENTENCE_END = re.compile(r"(?<=[.!?؟۔])\s+|\n+")


@dataclass
class ChunkOut:
    index: int
    content: str
    page_start: int | None
    page_end: int | None
    section_title: str | None
    chapter_title: str | None
    language: str
    extraction_method: str
    extraction_confidence: float
    content_hash: str
    token_estimate: int
    kind: str = "text"
    meta: dict = field(default_factory=dict)


@dataclass
class _Unit:
    text: str
    page: int | None
    kind: str
    method: str
    confidence: float
    section: str | None
    chapter: str | None
    meta: dict


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split an oversized text at sentence/line boundaries, then hard-split as a last resort."""
    parts, current = [], ""
    for piece in _SENTENCE_END.split(text):
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > max_chars:
            if current:
                parts.append(current)
                current = ""
            for i in range(0, len(piece), max_chars):
                parts.append(piece[i:i + max_chars])
            continue
        if len(current) + len(piece) + 1 > max_chars:
            parts.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip() if current else piece
    if current:
        parts.append(current)
    return parts


def _split_table(text: str, max_chars: int) -> list[str]:
    """Split a Markdown table by rows, repeating the header so every part stays self-describing."""
    lines = text.split("\n")
    if len(lines) < 4 or not lines[1].lstrip().startswith("|"):
        return _split_long(text, max_chars)
    header = "\n".join(lines[:2])
    parts, current = [], header
    for row in lines[2:]:
        if len(current) + len(row) + 1 > max_chars and current != header:
            parts.append(current)
            current = header
        current += "\n" + row
    if current != header:
        parts.append(current)
    return parts


def _units(pages: list[PageResult]) -> list[_Unit]:
    units: list[_Unit] = []
    section: str | None = None
    chapter: str | None = None
    for page in pages:
        if not page.has_text or page.failed:
            continue
        blocks = page.blocks or [Block("paragraph", page.text, page.page, 0, page.confidence)]
        for block in blocks:
            text = block.text.strip()
            if not text:
                continue
            if block.kind == "heading":
                is_chapter = bool(block.meta.get("chapter")) or block.level == 1
                if is_chapter:
                    chapter, section = text[:300], None
                else:
                    section = text[:300]
            conf = round(min(block.confidence, page.confidence) if page.confidence else block.confidence, 4)
            units.append(_Unit(text, page.page if block.page is None else block.page, block.kind, page.method, conf,
                               section, chapter, dict(block.meta)))
    return units


def _aggregate(units: list[_Unit]) -> tuple[str, float]:
    weights: Counter[str] = Counter()
    total, score = 0, 0.0
    for u in units:
        weights[u.method] += len(u.text)
        total += len(u.text)
        score += u.confidence * len(u.text)
    methods = sorted(weights)
    method = methods[0] if len(methods) == 1 else "+".join(methods)
    return method, round(score / total, 4) if total else 0.0


def chunk_result(result: ExtractionResult, max_chars: int = 1400, overlap_chars: int = 180,
                 min_chars: int = 120) -> list[ChunkOut]:
    if max_chars < 200 or not 0 <= overlap_chars < max_chars:
        raise ValueError("Require max_chars >= 200 and 0 <= overlap_chars < max_chars")
    units = _units(result.pages)
    chunks: list[ChunkOut] = []
    seen_hashes: set[str] = set()
    group: list[_Unit] = []
    size = 0
    tail_overlap = ""

    def emit(final_units: list[_Unit], prefix: str = "") -> str:
        nonlocal tail_overlap
        if not final_units:
            return ""
        body = "\n\n".join(u.text for u in final_units).strip()
        content = (prefix + "\n" + body).strip() if prefix else body
        digest = _hash(content)
        if digest in seen_hashes:
            return body[-overlap_chars:] if overlap_chars else ""
        seen_hashes.add(digest)
        pages = [u.page for u in final_units if u.page is not None]
        method, conf = _aggregate(final_units)
        kinds = {u.kind for u in final_units}
        kind = "table" if kinds == {"table"} else "equation" if kinds == {"equation"} else "text"
        extra: dict = {}
        formulas = sorted({f for u in final_units for f in u.meta.get("chemical_formulas", [])})
        if formulas:
            extra["chemical_formulas"] = formulas
        if "equation" in kinds:
            extra["contains_equations"] = True
        if "table" in kinds:
            extra["contains_tables"] = True
        if any(u.kind == "question" for u in final_units):
            extra["contains_questions"] = True
        if any(u.kind == "example" for u in final_units):
            extra["contains_examples"] = True
        if any(u.kind == "caption" for u in final_units):
            extra["contains_captions"] = True
        chunks.append(ChunkOut(len(chunks), content, min(pages) if pages else None, max(pages) if pages else None,
                               final_units[0].section, final_units[0].chapter, detect_language(content), method, conf,
                               digest, max(1, len(content.split())), kind, extra))
        return body[-overlap_chars:] if overlap_chars else ""

    def flush() -> None:
        nonlocal group, size, tail_overlap
        if group:
            tail_overlap = emit(group, "")
        group, size = [], 0

    for unit in units:
        pieces = [unit.text]
        if len(unit.text) > max_chars:
            pieces = _split_table(unit.text, max_chars) if unit.kind == "table" else _split_long(unit.text, max_chars)
        for piece in pieces:
            u = _Unit(piece, unit.page, unit.kind, unit.method, unit.confidence, unit.section, unit.chapter, unit.meta)
            boundary = bool(group) and (u.chapter != group[-1].chapter or
                                        (u.kind == "heading" and size >= min_chars) or
                                        (u.section != group[-1].section and size >= min_chars))
            if group and (size + len(u.text) + 2 > max_chars or boundary):
                crossed = boundary and (u.chapter != group[-1].chapter or u.section != group[-1].section)
                flush()
                if crossed:
                    tail_overlap = ""  # never leak overlap across a chapter/section boundary
            if not group and tail_overlap and u.kind not in {"heading", "table"} and len(u.text) + len(tail_overlap) < max_chars - 2:
                group.append(_Unit(tail_overlap, u.page, "overlap", u.method, u.confidence, u.section, u.chapter, {}))
                size += len(tail_overlap) + 2
                tail_overlap = ""
            group.append(u)
            size += len(u.text) + 2
    flush()
    return chunks
