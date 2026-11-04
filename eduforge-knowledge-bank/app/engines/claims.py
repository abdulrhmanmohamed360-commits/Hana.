"""Claim extraction from structured educational content, plus deterministic math verification of claims.

Pure Python (no database / framework imports). A claim's verdict from this module never involves a language model.
Statuses returned here: MATH_VERIFIED, REJECTED, UNVERIFIED (could not be checked deterministically).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.engines.math_verify import MathInputError, parse_expression, verify_equality, verify_solution, \
    verify_solution_steps
from app.engines.text_ar import normalize_arabic

_SENT_SPLIT = re.compile(r"(?<=[.!?؟؛])\s+|\n+")
_NUMERIC_EQ = re.compile(r"[0-9٠-٩][0-9٠-٩+\-*/^().×÷\s]*=\s*[0-9٠-٩+\-*/^().×÷\s]*[0-9٠-٩]")
_DEF_MARKERS = re.compile(r"(\bis defined as\b|\bis called\b|\brefers to\b|\bis a\b|\bis an\b|\bis the\b|"
                          r"يعرف|تعريف|يسمى|تسمى|هو |هي |يقصد ب)", re.I)


@dataclass
class ClaimCandidate:
    text: str
    claim_type: str          # values of app.models.enums.ClaimType
    kind: str                # text | math_solution | math_steps | math_equality | math_distractor
    item_ref: str            # which package item the claim came from
    payload: dict = field(default_factory=dict)
    extraction_method: str = "structured"


@dataclass(frozen=True)
class MathResult:
    status: str              # MATH_VERIFIED | REJECTED | UNVERIFIED
    detail: str
    method: str = "sympy"


def split_sentences(text: str, *, min_len: int = 12, max_sentences: int = 40) -> list[str]:
    out = [s.strip() for s in _SENT_SPLIT.split(text or "") if len(s.strip()) >= min_len]
    return out[:max_sentences]


def classify_sentence(sentence: str) -> str:
    return "DEFINITION" if _DEF_MARKERS.search(sentence) else "EXPLANATION"


def extract_text_claims(text: str, item_ref: str, *, default_type: str = "EXPLANATION",
                        method: str = "sentence_split") -> list[ClaimCandidate]:
    """One claim per sentence. Pure-numeric identities (e.g. '2+3 = 5') become deterministic math claims."""
    claims: list[ClaimCandidate] = []
    for sentence in split_sentences(text):
        eq = _NUMERIC_EQ.search(sentence)
        if eq and not re.search(r"[A-Za-zسص]", normalize_arabic(eq.group(0))):
            lhs, rhs = eq.group(0).split("=", 1)
            if any(op in lhs for op in "+-*/^×÷"):
                claims.append(ClaimCandidate(sentence, "FORMULA", "math_equality", item_ref,
                                             {"lhs": lhs.strip(), "rhs": rhs.strip()}, method))
                continue
        ctype = classify_sentence(sentence) if default_type in ("EXPLANATION", "DEFINITION") else default_type
        claims.append(ClaimCandidate(sentence, ctype, "text", item_ref, {}, method))
    return claims


def _solution_claim(equation: str, variable: str, answer: str, ref: str, ctype: str) -> ClaimCandidate:
    return ClaimCandidate(f"{equation} ⇒ {variable} = {answer}", ctype, "math_solution", ref,
                          {"equation": equation, "variable": variable, "answer": str(answer)})


def candidates_from_solved_example(data: dict, ref: str) -> list[ClaimCandidate]:
    eqs = [data["problem"]] + [s["equation"] for s in data.get("steps", [])]
    out = [ClaimCandidate(" → ".join(eqs), "EXAMPLE", "math_steps", ref, {"equations": eqs}),
           _solution_claim(data["problem"], data["variable"], data["answer"], ref, "EXAMPLE")]
    return out


def candidates_from_exercise(data: dict, ref: str) -> list[ClaimCandidate]:
    out = [_solution_claim(data["equation"], data["variable"], data["answer"], ref, "EXAMPLE")]
    steps = data.get("steps") or []
    if steps:
        eqs = [data["equation"]] + [s["equation"] for s in steps]
        out.append(ClaimCandidate(" → ".join(eqs), "EXAMPLE", "math_steps", ref, {"equations": eqs}))
    return out


def candidates_from_mcq(data: dict, ref: str) -> list[ClaimCandidate]:
    out: list[ClaimCandidate] = []
    for option in data["options"]:
        if option["label"] == data["correct_label"]:
            out.append(_solution_claim(data["equation"], data["variable"], option["value"], ref, "EXAMPLE"))
        else:
            out.append(ClaimCandidate(f"{data['equation']} ⇏ {data['variable']} = {option['value']}", "EXAMPLE",
                                      "math_distractor", ref,
                                      {"equation": data["equation"], "variable": data["variable"],
                                       "answer": str(option["value"])}))
    return out


def candidates_for_item(content_type: str, data: dict | None, body: str, ref: str) -> list[ClaimCandidate]:
    """Structured data wins; free text falls back to sentence-level claims."""
    data = data or {}
    out: list[ClaimCandidate] = []
    if content_type == "solved_examples" and data.get("problem"):
        out += candidates_from_solved_example(data, ref)
    elif content_type == "exercises" and data.get("equation"):
        out += candidates_from_exercise(data, ref)
    elif content_type == "questions" and data.get("options"):
        out += candidates_from_mcq(data, ref)
    elif content_type in ("explanation", "definitions", "summary", "formulas"):
        default = "DEFINITION" if content_type == "definitions" else "EXPLANATION"
        out += extract_text_claims(body, ref, default_type=default)
    return out


def verify_math_candidate(c: ClaimCandidate) -> MathResult | None:
    """Return a deterministic verdict for math claims; None for plain text claims."""
    p = c.payload
    if c.kind == "math_solution":
        v = verify_solution(p["equation"], p["variable"], p["answer"])
        if v.status == "CORRECT":
            return MathResult("MATH_VERIFIED", v.detail)
        if v.status == "INCORRECT":
            return MathResult("REJECTED", f"{v.detail}; solution set is {v.expected}")
        return MathResult("UNVERIFIED", v.detail)
    if c.kind == "math_distractor":
        v = verify_solution(p["equation"], p["variable"], p["answer"])
        if v.status == "INCORRECT":
            return MathResult("MATH_VERIFIED", "distractor is not a solution, as required")
        if v.status == "CORRECT":
            return MathResult("REJECTED", "a distractor is also a correct answer (ambiguous question)")
        return MathResult("UNVERIFIED", v.detail)
    if c.kind == "math_steps":
        verdicts = verify_solution_steps(p["equations"])
        if any(v.status == "INCORRECT" for v in verdicts):
            bad = next(v for v in verdicts if v.status == "INCORRECT")
            return MathResult("REJECTED", bad.detail)
        if any(v.status == "UNPARSEABLE" for v in verdicts):
            bad = next(v for v in verdicts if v.status == "UNPARSEABLE")
            return MathResult("UNVERIFIED", bad.detail)
        return MathResult("MATH_VERIFIED", f"{len(verdicts)} steps preserve the solution set")
    if c.kind == "math_equality":
        v = verify_equality(p["lhs"], p["rhs"])
        return {"CORRECT": MathResult("MATH_VERIFIED", v.detail),
                "INCORRECT": MathResult("REJECTED", v.detail)}.get(v.status, MathResult("UNVERIFIED", v.detail))
    return None
