"""Deterministic text chunking that retains original page provenance."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ingestion import content_hash, normalize_text


@dataclass(frozen=True)
class TextSegment:
    text: str
    page: int | None = None


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    page_start: int | None
    page_end: int | None
    content_hash: str
    token_estimate: int


def chunk_segments(segments: list[TextSegment], max_chars: int = 1400, overlap_chars: int = 180) -> list[Chunk]:
    """Split ordered segments with bounded overlap and accurate page spans."""
    if max_chars < 200 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("Require max_chars >= 200 and 0 <= overlap_chars < max_chars")
    pieces: list[str] = []
    page_ranges: list[tuple[int, int, int]] = []
    cursor = 0
    for segment in segments:
        value = normalize_text(segment.text)
        if not value:
            continue
        if pieces:
            pieces.append("\n\n")
            cursor += 2
        begin = cursor
        pieces.append(value)
        cursor += len(value)
        if segment.page is not None:
            page_ranges.append((begin, cursor, segment.page))
    full_text = "".join(pieces)
    if not full_text:
        return []

    result: list[Chunk] = []
    start = 0
    while start < len(full_text):
        hard_end = min(start + max_chars, len(full_text))
        end = hard_end
        if hard_end < len(full_text):
            # Prefer a paragraph/word boundary without producing tiny chunks.
            candidates = [full_text.rfind("\n", start + max_chars // 2, hard_end),
                          full_text.rfind(" ", start + max_chars // 2, hard_end)]
            boundary = max(candidates)
            if boundary > start:
                end = boundary
        content = normalize_text(full_text[start:end])
        if content:
            pages = [page for left, right, page in page_ranges if left < end and right > start]
            result.append(Chunk(len(result), content, min(pages) if pages else None,
                                max(pages) if pages else None, content_hash(content),
                                max(1, len(content.split()))))
        if end >= len(full_text):
            break
        next_start = max(start + 1, end - overlap_chars)
        # Avoid beginning the next passage in the middle of a word.
        space = full_text.find(" ", next_start, min(next_start + 40, len(full_text)))
        start = space + 1 if space >= 0 else next_start
    return result
