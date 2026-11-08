"""Per-page extraction quality scoring. Decides when a page needs a second engine or OCR."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .arabic import assess_arabic, script_counts

_CID = re.compile(r"\(cid:\d+\)")
_WORD = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)


@dataclass
class PageQuality:
    score: float
    char_count: int
    needs_better: bool
    issues: list[str] = field(default_factory=list)


def evaluate_text(text: str, *, min_chars: int = 25, min_quality: float = 0.55) -> PageQuality:
    """Score 0..1. Low scores mean: missing, garbled, or mostly non-linguistic text."""
    stripped = (text or "").strip()
    n = len(stripped)
    issues: list[str] = []
    if n == 0:
        return PageQuality(0.0, 0, True, ["no_text"])
    if n < min_chars:
        issues.append("insufficient_text")

    letters = sum(1 for ch in stripped if ch.isalpha())
    digits = sum(1 for ch in stripped if ch.isdigit())
    spaces = sum(1 for ch in stripped if ch.isspace())
    other = n - letters - digits - spaces
    linguistic = (letters + digits) / n
    score = 0.25 + 0.75 * min(1.0, linguistic / 0.7)

    garbage = stripped.count("\ufffd") + 3 * len(_CID.findall(stripped)) + sum(1 for ch in stripped if "\ue000" <= ch <= "\uf8ff")
    if garbage / n > 0.01:
        issues.append("garbled_glyphs")
        score -= min(0.7, garbage / n * 10)

    words = _WORD.findall(stripped)
    if words:
        avg = sum(len(w) for w in words) / len(words)
        if avg < 1.8 and len(words) > 20:
            issues.append("fragmented_words")
            score -= 0.25
    elif letters == 0:
        issues.append("no_words")
        score -= 0.3
    if other / n > 0.5:
        issues.append("symbol_noise")
        score -= 0.2

    ar_q = assess_arabic(stripped)
    if ar_q.arabic_chars >= 20 and ar_q.score < 1.0:
        issues.extend(f"arabic:{i}" for i in ar_q.issues)
        score -= (1.0 - ar_q.score) * 0.8

    if n < min_chars:
        score = min(score, 0.45)
    score = round(max(0.0, min(1.0, score)), 4)
    return PageQuality(score, n, score < min_quality or n < min_chars, issues)


def text_density_ok(text: str, expected_min_chars: int) -> bool:
    return len((text or "").strip()) >= expected_min_chars


def has_arabic(text: str) -> bool:
    return script_counts(text)[0] > 0
