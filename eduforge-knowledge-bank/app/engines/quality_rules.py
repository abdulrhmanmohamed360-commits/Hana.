"""Knowledge quality scoring (pure). Every dimension returns a score AND a human-readable reason.

Two things are deliberately kept apart:
  * support_score  — how well-supported / reviewed / fresh the knowledge is (a confidence-style number);
  * correctness    — what was actually *checked*: only deterministic verification can yield DETERMINISTICALLY_VERIFIED.
A high support_score never implies the content is true, and the numbers are never used as a truth verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field

RUBRIC_VERSION = "1"
WEIGHTS = {"verification": 0.25, "source_reliability": 0.15, "evidence_strength": 0.15, "consistency": 0.15,
           "completeness": 0.10, "freshness": 0.10, "review": 0.10}
_REVIEW = {"APPROVED": 1.0, "REVIEW": 0.5, "DRAFT": 0.2, "REJECTED": 0.0, "SUPERSEDED": 0.0}


@dataclass
class EvidenceFact:
    authority: float
    usable: bool
    group: str
    stance: str = "SUPPORTS"        # SUPPORTS | OPPOSES | NEUTRAL


@dataclass
class ClaimFact:
    verdict: str                    # MATH_VERIFIED | SOURCE_SUPPORTED | UNVERIFIED | CONFLICTED | REJECTED
    evidence: list[EvidenceFact] = field(default_factory=list)


@dataclass
class QualityInput:
    review_status: str
    claims: list[ClaimFact] = field(default_factory=list)
    source_authorities: list[float] = field(default_factory=list)   # registered, ALLOWED sources cited by the asset
    cited_sources: int = 0
    completeness: float | None = None
    contradiction_edges: int = 0
    age_days: float | None = None
    freshness_days: int = 730


@dataclass
class Dimension:
    score: float | None
    reason: str


@dataclass
class Assessment:
    dimensions: dict[str, Dimension]
    support_score: float
    correctness: str                # DETERMINISTICALLY_VERIFIED | REJECTED | CONFLICTED | UNKNOWN
    correctness_basis: str
    flags: list[str]
    needs_human_review: bool
    note: str = ("support_score measures sourcing, verification coverage, consistency, freshness and review; "
                 "it is not the probability that the content is correct.")


def assess(i: QualityInput) -> Assessment:
    d: dict[str, Dimension] = {}
    flags: list[str] = []
    n = len(i.claims)
    count = lambda v: sum(1 for c in i.claims if c.verdict == v)  # noqa: E731

    # source reliability: operator-assigned authority of registered, ALLOWED sources actually cited
    if i.source_authorities:
        top = sorted(i.source_authorities, reverse=True)[:3]
        d["source_reliability"] = Dimension(round(sum(top) / len(top), 3),
                                            f"mean authority of {len(top)} registered source(s): {top}")
    elif i.cited_sources:
        d["source_reliability"] = Dimension(0.2, f"{i.cited_sources} cited source(s) but none registered as ALLOWED")
        flags.append("SOURCES_NOT_REGISTERED")
    else:
        d["source_reliability"] = Dimension(0.0, "no sources cited")
        flags.append("NO_SOURCES")

    # verification coverage
    if n == 0:
        d["verification"] = Dimension(0.0, "no claims were extracted/verified for this content")
        flags.append("NO_VERIFIED_CLAIMS")
    elif count("REJECTED"):
        d["verification"] = Dimension(0.0, f"{count('REJECTED')} of {n} claim(s) FAILED verification")
        flags.append("HAS_REJECTED_CLAIMS")
    else:
        s = (count("MATH_VERIFIED") + 0.5 * count("SOURCE_SUPPORTED")) / n
        d["verification"] = Dimension(round(s, 3), f"{count('MATH_VERIFIED')} deterministic, {count('SOURCE_SUPPORTED')} "
                                                   f"source-supported, {count('UNVERIFIED')} unverified of {n}")
        if count("UNVERIFIED"):
            flags.append("HAS_UNVERIFIED_CLAIMS")

    # evidence strength: independent supporting origins; opposing origins subtract
    groups_for = {e.group for c in i.claims for e in c.evidence if e.usable and e.stance == "SUPPORTS"}
    groups_against = {e.group for c in i.claims for e in c.evidence if e.usable and e.stance == "OPPOSES"}
    strong = count("MATH_VERIFIED") == n and n > 0
    base = 1.0 if strong else {0: 0.0, 1: 0.5, 2: 0.8}.get(len(groups_for), 1.0)
    es = max(0.0, base - 0.3 * len(groups_against))
    d["evidence_strength"] = Dimension(round(es, 3), (
        "all claims deterministically checked" if strong else
        f"{len(groups_for)} independent supporting source group(s), {len(groups_against)} opposing"))

    # consistency
    conflicts = count("CONFLICTED") + i.contradiction_edges
    d["consistency"] = Dimension(round(max(0.0, 1.0 - 0.5 * conflicts), 3),
                                 "no conflicts recorded" if not conflicts else
                                 f"{count('CONFLICTED')} conflicted claim(s) + {i.contradiction_edges} CONTRADICTS edge(s)")
    if conflicts:
        flags.append("CONFLICTS")

    d["completeness"] = (Dimension(round(i.completeness, 3), "share of lesson requirements met by approved content")
                         if i.completeness is not None else Dimension(None, "not applicable to this entity"))

    if i.age_days is None:
        d["freshness"] = Dimension(None, "age unknown")
    elif i.age_days <= i.freshness_days:
        d["freshness"] = Dimension(1.0, f"{int(i.age_days)} days old (window {i.freshness_days})")
    else:
        s = max(0.0, 1.0 - (i.age_days - i.freshness_days) / i.freshness_days)
        d["freshness"] = Dimension(round(s, 3), f"{int(i.age_days)} days old, beyond the {i.freshness_days}-day window")
        flags.append("OUTDATED")

    d["review"] = Dimension(_REVIEW.get(i.review_status, 0.0), f"review status {i.review_status}")
    if i.review_status != "APPROVED":
        flags.append("NOT_APPROVED")

    scored = {k: v.score for k, v in d.items() if v.score is not None}
    wsum = sum(WEIGHTS[k] for k in scored)
    support = round(sum(WEIGHTS[k] * s for k, s in scored.items()) / wsum, 3) if wsum else 0.0

    if count("REJECTED"):
        correctness, basis = "REJECTED", "a deterministic check failed"
    elif count("CONFLICTED") or i.contradiction_edges:
        correctness, basis = "CONFLICTED", "sources or graph edges contradict this content"
    elif n and count("MATH_VERIFIED") == n:
        correctness, basis = "DETERMINISTICALLY_VERIFIED", f"all {n} claim(s) passed a SymPy check"
    else:
        correctness, basis = "UNKNOWN", "not all claims could be checked deterministically; human review needed"
    return Assessment(d, support, correctness, basis, flags,
                      needs_human_review=i.review_status != "APPROVED" or correctness in ("UNKNOWN", "CONFLICTED"))
