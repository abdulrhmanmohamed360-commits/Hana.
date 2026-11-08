"""Lesson completeness rules (pure). A lesson is never 'complete' because it has a title or a short text:
each requirement states a minimum count, minimum length per asset, sources, and answers, and only APPROVED assets count."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requirement:
    asset_type: str
    min_count: int = 1
    min_chars: int = 0
    require_sources: bool = False
    require_answers: bool = False


@dataclass(frozen=True)
class Profile:
    key: str
    requirements: tuple[Requirement, ...]
    freshness_days: int | None = 730


@dataclass
class AssetFacts:
    id: str
    asset_type: str
    status: str                      # DRAFT | REVIEW | APPROVED | REJECTED | SUPERSEDED
    chars: int
    has_sources: bool
    items: int = 0                   # structured sub-items (exercises/questions) if known
    items_with_answers: int = 0
    age_days: float | None = None
    unverified_claims: int = 0
    rejected_claims: int = 0


@dataclass
class LessonCoverage:
    status: str                      # MISSING | INCOMPLETE | PENDING_REVIEW | OUTDATED | COMPLETE
    score: float                     # share of requirements met by APPROVED content only
    met: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unmet: list[dict] = field(default_factory=list)
    outdated_assets: list[str] = field(default_factory=list)
    unverified_claims: int = 0
    pending_reviews: int = 0
    reasons: list[str] = field(default_factory=list)


PROFILES: dict[str, Profile] = {
    "default": Profile("default", (
        Requirement("explanation", 1, 200, require_sources=True),
        Requirement("example", 1, 40),
        Requirement("exercise", 1, 20, require_answers=True),
        Requirement("summary", 1, 60))),
    "mathematics": Profile("mathematics", (
        Requirement("explanation", 1, 200, require_sources=True),
        Requirement("definition", 1, 30),
        Requirement("example", 2, 40),
        Requirement("exercise", 3, 20, require_answers=True),
        Requirement("quiz", 1, 20, require_answers=True),
        Requirement("summary", 1, 60))),
    "humanities": Profile("humanities", (
        Requirement("explanation", 1, 400, require_sources=True),
        Requirement("summary", 1, 80),
        Requirement("quiz", 1, 20))),
}
_MATH = ("math", "رياضيات", "algebra", "geometry", "جبر", "هندسة", "حساب")
_HUMAN = ("history", "geography", "تاريخ", "جغرافيا", "دراسات", "social", "arabic", "عربي", "لغه", "language")


def pick_profile_key(subject: str) -> str:
    from app.engines.text_ar import normalize_arabic
    s = normalize_arabic(subject).casefold()
    if any(k in s for k in _MATH):
        return "mathematics"
    if any(k in s for k in _HUMAN):
        return "humanities"
    return "default"


def _valid(req: Requirement, a: AssetFacts) -> tuple[bool, str]:
    if a.chars < req.min_chars:
        return False, f"asset is too short ({a.chars} < {req.min_chars} characters)"
    if req.require_sources and not a.has_sources:
        return False, "asset cites no sources"
    if req.require_answers:
        if a.items == 0:
            return False, "answers cannot be checked: asset has no structured items"
        if a.items_with_answers < a.items:
            return False, f"{a.items - a.items_with_answers} of {a.items} items have no answer"
    if a.rejected_claims:
        return False, f"{a.rejected_claims} claim(s) failed verification"
    return True, ""


def _units(assets: list[AssetFacts]) -> int:
    """A structured asset holding 5 exercises counts as 5 units; a plain asset counts as 1."""
    return sum(max(1, a.items) for a in assets)


def evaluate_lesson(profile: Profile, assets: list[AssetFacts]) -> LessonCoverage:
    live = [a for a in assets if a.status not in ("REJECTED", "SUPERSEDED")]
    cov = LessonCoverage(status="MISSING", score=0.0)
    if not live:
        cov.reasons.append("no learning assets are linked to this lesson")
        cov.unmet = [{"asset_type": r.asset_type, "reason": "no asset"} for r in profile.requirements]
        return cov
    stale: set[str] = set()
    for req in profile.requirements:
        same = [a for a in live if a.asset_type == req.asset_type]
        approved, reasons = [], []
        for a in (x for x in same if x.status == "APPROVED"):
            ok, why = _valid(req, a)
            (approved.append(a) if ok else reasons.append(why))
        review = []
        for a in (x for x in same if x.status in ("DRAFT", "REVIEW")):
            ok, why = _valid(req, a)
            (review.append(a) if ok else reasons.append(f"draft: {why}"))
        if _units(approved) >= req.min_count:
            cov.met.append(req.asset_type)
            if profile.freshness_days:
                stale |= {a.id for a in approved if a.age_days is not None and a.age_days > profile.freshness_days}
            cov.unverified_claims += sum(a.unverified_claims for a in approved)
        elif _units(approved) + _units(review) >= req.min_count:
            cov.pending.append(req.asset_type)
            cov.pending_reviews += len(review)
        else:
            if not same:
                reasons = ["no asset"]
            elif approved or review:
                reasons.append(f"needs {req.min_count}, has {_units(approved) + _units(review)} usable")
            cov.unmet.append({"asset_type": req.asset_type, "reason": "; ".join(dict.fromkeys(reasons)) or "insufficient"})
    cov.outdated_assets = sorted(stale)
    cov.score = round(len(cov.met) / len(profile.requirements), 4)
    if cov.unmet:
        cov.status = "INCOMPLETE"
    elif cov.pending:
        cov.status = "PENDING_REVIEW"
    elif stale:
        cov.status = "OUTDATED"
    else:
        cov.status = "COMPLETE"
    cov.reasons = [f"requirement '{u['asset_type']}' not met: {u['reason']}" for u in cov.unmet] + \
                  [f"requirement '{p}' is only satisfied by unapproved drafts" for p in cov.pending] + \
                  [f"{len(stale)} approved asset(s) older than {profile.freshness_days} days" for _ in [0] if stale]
    return cov


def summarize(statuses: list[str], scores: list[float]) -> dict:
    total = len(statuses)
    count = lambda s: sum(1 for x in statuses if x == s)  # noqa: E731
    return {"total_lessons": total, "covered_lessons": count("COMPLETE"), "missing_lessons": count("MISSING"),
            "incomplete_lessons": count("INCOMPLETE"), "pending_review_lessons": count("PENDING_REVIEW"),
            "outdated_lessons": count("OUTDATED"),
            "coverage_percentage": round(100 * count("COMPLETE") / total, 2) if total else 0.0,
            "average_requirement_score": round(sum(scores) / total, 4) if total else 0.0}
