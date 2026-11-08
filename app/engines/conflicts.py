"""Heuristic claim-conflict detection. It FLAGS candidates for review; it never decides which source is right."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.engines.text_ar import normalize_arabic, tokenize

_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_STOP = {"the", "is", "are", "of", "a", "an", "in", "at", "to", "and", "هو", "هي", "في", "من", "على", "ان", "الى"}
_NEG = {"not", "no", "never", "لا", "ليس", "ليست", "لم", "غير"}


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    source: str


@dataclass(frozen=True)
class Conflict:
    claim_a: str
    claim_b: str
    kind: str  # NUMERIC | NEGATION
    shared_terms: tuple[str, ...]
    detail: str


def _numbers(text: str) -> list[float]:
    return [float(n) for n in _NUM.findall(normalize_arabic(text))]


def _content_terms(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in _STOP and not t.replace(".", "").isdigit()}


def find_conflicts(claims: list[Claim], *, min_shared_terms: int = 2, min_overlap: float = 0.6) -> list[Conflict]:
    out: list[Conflict] = []
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if a.source == b.source:
                continue
            ta, tb = _content_terms(a.text), _content_terms(b.text)
            ta_core, tb_core = ta - _NEG, tb - _NEG
            shared = ta_core & tb_core
            if len(shared) < min_shared_terms or len(shared) / max(1, min(len(ta_core), len(tb_core))) < min_overlap:
                continue
            na, nb = _numbers(a.text), _numbers(b.text)
            if na and nb and set(na) != set(nb):
                out.append(Conflict(a.id, b.id, "NUMERIC", tuple(sorted(shared)),
                                    f"different values: {sorted(set(na))} vs {sorted(set(nb))}"))
            elif bool(ta & _NEG) != bool(tb & _NEG):
                out.append(Conflict(a.id, b.id, "NEGATION", tuple(sorted(shared)), "one claim negates the other"))
    return out

content_terms = _content_terms  # public alias used by the verification service
