"""Heuristic structure detection over plain text: headings, questions/exercises, equations, chemistry, captions, lists.
Everything is best-effort and carries a confidence; the raw text is never altered or dropped."""
from __future__ import annotations

import re
from collections import Counter

from .arabic import normalize_unicode
from .types import Block

_CHAPTER = re.compile(
    r"^\s*(?:(?:الفصل|الباب|الوحدة|الجزء|المحور)\s*(?:ال)?[\w\u0600-\u06ff]*\s*[:：\-–]?\s*[\d٠-٩]*|"
    r"(?:chapter|unit|part|module)\s+[\dIVXivx]+)\b", re.IGNORECASE)
_SECTION = re.compile(
    r"^\s*(?:(?:الدرس|المبحث|القسم|الموضوع)\s*[\w\u0600-\u06ff]*\s*[:：\-–]?\s*[\d٠-٩]*|"
    r"(?:lesson|section|topic)\s+[\dIVXivx]+|\d+(?:[.\-]\d+){1,3}[.)]?\s+\S)", re.IGNORECASE)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_QUESTION = re.compile(
    r"^\s*(?:(?:سؤال|س|تمرين|تدريب|مسألة|نشاط|أسئلة)\s*[\d٠-٩]*\s*[:：\-–.)]|"
    r"(?:question|exercise|problem|activity|quiz)\s*\d*\s*[:：\-–.)]|"
    r"[\d٠-٩]{1,3}\s*[.)\-–]\s+\S|[أ-ي]\s*[-–)]\s+\S|[a-dA-D]\s*[.)]\s+\S)", re.IGNORECASE)
_EXAMPLE = re.compile(r"^\s*(?:مثال|أمثلة|example|solved example|worked example)\s*[\d٠-٩]*\s*[:：\-–.]?", re.IGNORECASE)
_CAPTION = re.compile(r"^\s*(?:شكل|الشكل|صورة|جدول|الجدول|رسم|figure|fig\.|table|diagram|chart)\s*[\d٠-٩]+(?:[.\-]\d+)?\s*[:：\-–.]?",
                      re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[•●▪◦‣·\-*]\s+|[\d٠-٩]+[.)]\s+)")
_MATH_CHARS = set("=+−-×÷±∓≤≥≠≈∝∞∑∏∫∂∇√∛∜∈∉⊂⊃∪∩∧∨¬→←↔⇒⇔∀∃∠⊥∥°′″πθλμΩΔαβγδεσφψω^_/")
_MATH_STRONG = set("∑∏∫∂∇√∛∜±≤≥≠≈∞∈∉⊂⊃∪∩∀∃∠⊥∥×÷")
_SUBSCRIPT = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "01234567890123456789+-")
_CHEM = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?\d*(?:\([A-Za-z0-9]+\)\d*)?){2,}(?:[+-]|\d*[+-])?(?![A-Za-z])")
_REACTION = re.compile(r"(?:->|→|⇌|⟶|=>)")
_PHYSICS = re.compile(r"\b(?:[vFamgEPVIRQTtdsρΔ]\s*=\s*[^\s]+|\d+(?:\.\d+)?\s*(?:m/s²?|m/s|kg|N|J|W|Hz|Pa|V|A|Ω|K|mol|°C|cm|mm|km|g|s))\b")
_CHEM_STOPWORDS = {"OK", "ID", "PDF", "USA", "UK", "AM", "PM", "HTML", "CSS", "JSON", "CSV", "DNA", "RNA", "ATP", "TV", "UN", "EU"}


def _is_heading_candidate(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 110 or s.endswith((".", "،", ",", ";", "؛")) and not _CHAPTER.match(s):
        return False
    return True


def _math_ratio(line: str) -> float:
    s = line.translate(_SUBSCRIPT)
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if c in _MATH_CHARS or c.isdigit()) / len(chars)


def classify_line(line: str) -> tuple[str, int, float, dict]:
    """Return (kind, level, confidence, meta) for a single line."""
    s = normalize_unicode(line).strip()
    if not s:
        return "blank", 0, 1.0, {}
    md = _MD_HEADING.match(s)
    if md:
        return "heading", len(md.group(1)), 0.95, {"title": md.group(2)}
    if _CAPTION.match(s):
        return "caption", 0, 0.85, {}
    if _EXAMPLE.match(s):
        return "example", 0, 0.85, {}
    if _is_heading_candidate(s) and _CHAPTER.match(s):
        return "heading", 1, 0.85, {"chapter": True}
    if _is_heading_candidate(s) and _SECTION.match(s) and len(s.split()) <= 14:
        return "heading", 2, 0.7, {}
    if _QUESTION.match(s):
        return "question", 0, 0.6, {}
    strong = sum(1 for c in s if c in _MATH_STRONG)
    if (strong >= 1 and _math_ratio(s) > 0.2) or ("=" in s and _math_ratio(s) > 0.4 and len(s.split()) <= 12):
        return "equation", 0, 0.6, {}
    if _REACTION.search(s) and _CHEM.search(s.translate(_SUBSCRIPT)):
        return "equation", 0, 0.55, {"chemical": True}
    if _BULLET.match(s):
        return "list", 0, 0.7, {}
    return "paragraph", 0, 1.0, {}


def find_chemical_formulas(text: str) -> list[str]:
    found = []
    for m in _CHEM.finditer(text.translate(_SUBSCRIPT)):
        token = m.group(0)
        if token in _CHEM_STOPWORDS or len(token) < 2:
            continue
        if any(ch.isdigit() for ch in token) or token.istitle() is False and token[0].isupper():
            found.append(token)
    return sorted(set(found))[:30]


def analyze_text(text: str, page: int | None, *, base_confidence: float = 1.0) -> list[Block]:
    """Group lines into blocks. Consecutive paragraph lines merge; structural lines stand alone. Lossless: the
    concatenation of block texts (plus separators) always contains every non-blank input line."""
    blocks: list[Block] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            body = "\n".join(buffer).strip()
            if body:
                meta = {}
                chem = find_chemical_formulas(body)
                if chem:
                    meta["chemical_formulas"] = chem
                blocks.append(Block("paragraph", body, page, 0, round(base_confidence, 4), meta))
            buffer.clear()

    for raw in text.split("\n"):
        kind, level, conf, meta = classify_line(raw)
        if kind == "blank":
            flush()
            continue
        if kind == "paragraph":
            buffer.append(raw.strip())
            continue
        if kind in {"list", "question"} and blocks and blocks[-1].kind == kind and not buffer:
            prev = blocks[-1]
            prev.text += "\n" + raw.strip()
            continue
        flush()
        extra = dict(meta)
        if kind == "equation":
            extra["raw_preserved"] = True
        blocks.append(Block(kind, raw.strip(), page, level, round(min(base_confidence, conf), 4), extra))
    flush()
    return blocks


def table_to_markdown(rows: list[list[str | None]]) -> str:
    """Render rows as a Markdown table (header = first row). Cells are cleaned, never dropped."""
    cleaned = [[(c or "").replace("\n", " ").replace("|", "\\|").strip() for c in row] for row in rows if row]
    cleaned = [r for r in cleaned if any(r)]
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    lines = ["| " + " | ".join(cleaned[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in cleaned[1:]]
    return "\n".join(lines)


def strip_repeating_margins(pages_lines: list[list[str]], min_pages: int = 5, ratio: float = 0.6) -> set[str]:
    """Return normalized header/footer lines repeated on most pages (e.g. running titles, page numbers)."""
    if len(pages_lines) < min_pages:
        return set()
    counter: Counter[str] = Counter()
    for lines in pages_lines:
        edge = [ln for ln in lines[:2] + lines[-2:] if ln.strip()]
        seen = {re.sub(r"[\d٠-٩]+", "#", ln.strip()) for ln in edge if len(ln.strip()) < 80}
        counter.update(seen)
    limit = ratio * len(pages_lines)
    return {k for k, v in counter.items() if v >= limit}


def margin_key(line: str) -> str:
    return re.sub(r"[\d٠-٩]+", "#", line.strip())
