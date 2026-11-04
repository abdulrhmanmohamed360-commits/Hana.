"""Verification Service: connects Arabic normalization, SymPy math checks and source-conflict detection to the DB.

Verdict rules (explicit, auditable):
  MATH_VERIFIED    deterministic SymPy check passed (no language model involved)
  REJECTED         deterministic check failed (wrong answer / invalid step / ambiguous question)
  CONFLICTED       a retrieved passage from a usable source contradicts the claim (numeric or negation)
  SOURCE_SUPPORTED >= 60% of the claim's content terms appear in a passage of a usable, non-UNVERIFIED source and no
                   conflict was found. This is lexical support, NOT proof; it never becomes human-approved by itself.
  UNVERIFIED       everything else (including claims only matched by unreviewed web content)
"Usable source" = uploaded document without a Source row, or a Source whose policy is ALLOWED.
Nothing here ever sets a LearningAsset to APPROVED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from app.engines.claims import ClaimCandidate, split_sentences, verify_math_candidate
from app.engines.conflicts import Claim, content_terms, find_conflicts
from app.engines.text_ar import tokenize
from app.models import EvidenceRecord, ExtractedClaim, Source, VerificationJob
from app.models.enums import ClaimStatus, ClaimType, EvidenceStance, SourcePolicy, TaskStatus
from app.schemas.prepare import STATUS_SEVERITY, ClaimReport, ConflictReport
from app.services import audit
from app.services.retrieval import HybridHit, retrieve_hybrid

SUPPORT_THRESHOLD = 0.6
_TO_CLAIM_STATUS = {"MATH_VERIFIED": ClaimStatus.VERIFIED, "REJECTED": ClaimStatus.REJECTED,
                    "CONFLICTED": ClaimStatus.CONFLICTED, "SOURCE_SUPPORTED": ClaimStatus.PENDING_VERIFICATION,
                    "UNVERIFIED": ClaimStatus.PENDING_VERIFICATION}


@dataclass
class EvidenceMatch:
    hit: HybridHit
    coverage: float
    usable: bool
    stance: EvidenceStance
    authority: float


@dataclass
class TextVerdict:
    status: str
    detail: str
    matches: list[EvidenceMatch] = field(default_factory=list)
    conflicts: list = field(default_factory=list)


def worst_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: STATUS_SEVERITY[s]) if statuses else "UNVERIFIED"


def _source_of(db: Session, hit: HybridHit) -> Source | None:
    return db.get(Source, hit.document.source_id) if hit.document.source_id else None


def _is_usable(db: Session, hit: HybridHit) -> bool:
    """Reviewed-source test: no unverified-content flag, and the source (if any) is explicitly ALLOWED."""
    flagged = ((hit.chunk.meta or {}).get("verification_status") == "UNVERIFIED"
               or (hit.document.meta or {}).get("verification_status") == "UNVERIFIED_SOURCE_CONTENT")
    if flagged:
        return False
    source = _source_of(db, hit)
    return source is None or source.policy == SourcePolicy.ALLOWED


def verify_text_claim(db: Session, text: str, *, hits: list[HybridHit] | None = None) -> TextVerdict:
    claim_terms = content_terms(text)
    if len(claim_terms) < 2:
        return TextVerdict("UNVERIFIED", "claim too short to compare against sources")
    hits = hits if hits is not None else retrieve_hybrid(db, text, limit=4).hits
    matches: list[EvidenceMatch] = []
    sentences: list[Claim] = [Claim("claim", text, "generated")]
    for hit in hits:
        chunk_terms = set(tokenize(hit.chunk.content))
        coverage = len(claim_terms & chunk_terms) / len(claim_terms)
        if coverage < 0.3:
            continue
        source = _source_of(db, hit)
        matches.append(EvidenceMatch(hit, round(coverage, 3), _is_usable(db, hit), EvidenceStance.NEUTRAL,
                                     source.authority_score if source else 0.5))
        if _is_usable(db, hit):
            for n, sentence in enumerate(split_sentences(hit.chunk.content, min_len=20)[:30]):
                if len(claim_terms & content_terms(sentence)) >= 2:
                    sentences.append(Claim(f"{hit.document.id}:{n}", sentence, str(hit.document.id)))
    conflicts = [c for c in find_conflicts(sentences) if "claim" in (c.claim_a, c.claim_b)]
    if conflicts:
        opposing = {c.claim_a if c.claim_b == "claim" else c.claim_b for c in conflicts}
        for m in matches:
            if any(o.startswith(str(m.hit.document.id)) for o in opposing):
                m.stance = EvidenceStance.OPPOSES
        return TextVerdict("CONFLICTED", f"{len(conflicts)} source passage(s) disagree: {conflicts[0].detail}",
                           matches, conflicts)
    supporting = [m for m in matches if m.usable and m.coverage >= SUPPORT_THRESHOLD]
    for m in supporting:
        m.stance = EvidenceStance.SUPPORTS
    if supporting:
        best = max(m.coverage for m in supporting)
        return TextVerdict("SOURCE_SUPPORTED", f"lexical support {best:.0%} from {len(supporting)} usable passage(s)",
                           matches)
    if matches:
        return TextVerdict("UNVERIFIED", "only partial or unreviewed-source overlap found", matches)
    return TextVerdict("UNVERIFIED", "no supporting passage found in the knowledge base")


def verify_and_record(db: Session, candidates: list[ClaimCandidate], *, asset_id: UUID, actor: str,
                      domain_id: UUID | None = None) -> list[ClaimReport]:
    """Verify every candidate, persist claims + evidence + a verification job each, and audit the batch."""
    reports: list[ClaimReport] = []
    for cand in candidates:
        math = verify_math_candidate(cand)
        matches: list[EvidenceMatch] = []
        if math is not None:
            status, method, detail = math.status, "sympy_deterministic", math.detail
            confidence, evidence_score = (1.0 if status in ("MATH_VERIFIED", "REJECTED") else 0.0), 0.0
        else:
            verdict = verify_text_claim(db, cand.text)
            status, method, detail, matches = verdict.status, "source_overlap_and_conflict_check", verdict.detail, verdict.matches
            evidence_score = max((m.coverage for m in matches if m.usable), default=0.0)
            confidence = min(0.7, evidence_score * 0.7) if status == "SOURCE_SUPPORTED" else 0.0
        claim = ExtractedClaim(
            raw_document_id=None, learning_asset_id=asset_id, domain_id=domain_id, claim_text=cand.text[:8000],
            claim_type=ClaimType(cand.claim_type) if cand.claim_type in ClaimType.__members__ else ClaimType.OTHER,
            status=_TO_CLAIM_STATUS[status], confidence=confidence, evidence_score=evidence_score,
            extraction_method=cand.extraction_method,
            meta={"verification_status": status, "method": method, "detail": detail[:1000],
                  "kind": cand.kind, "item_ref": cand.item_ref})
        db.add(claim)
        db.flush()
        for m in matches:
            db.add(EvidenceRecord(
                claim_id=claim.id, source_id=m.hit.document.source_id, raw_document_id=m.hit.document.id,
                stance=m.stance, independence_group=str(m.hit.document.source_id or m.hit.document.content_hash)[:128],
                authority_score=m.authority, relevance_score=min(1.0, m.coverage),
                excerpt=m.hit.chunk.content[:1500],
                methodology=f"lexical coverage {m.coverage:.2f}; usable={m.usable}; page={m.hit.chunk.page_start}"))
        db.add(VerificationJob(claim_id=claim.id, status=TaskStatus.SUCCEEDED, attempts=1, methodology=method,
                               result={"status": status, "detail": detail[:1000], "evidence": len(matches)}))
        reports.append(ClaimReport(claim_id=str(claim.id), text=cand.text, claim_type=cand.claim_type,
                                   verification_status=status, method=method, detail=detail,
                                   evidence_count=len(matches)))
    audit.record(db, actor=actor, action="claims.verify", entity_type="learning_asset", entity_id=asset_id,
                 details={"claims": len(reports),
                          "by_status": {s: sum(1 for r in reports if r.verification_status == s)
                                        for s in {r.verification_status for r in reports}}})
    db.flush()
    return reports


def conflicts_between_sources(hits: list[HybridHit], limit: int = 60) -> list[ConflictReport]:
    """Cross-document disagreement inside the evidence retrieved for one lesson (flags; never resolves)."""
    sentences: list[Claim] = []
    titles: dict[str, str] = {}
    for hit in hits:
        doc_id = str(hit.document.id)
        titles[doc_id] = hit.document.title or doc_id
        for n, s in enumerate(split_sentences(hit.chunk.content, min_len=20)):
            sentences.append(Claim(f"{doc_id}:{hit.chunk.chunk_index}:{n}", s, doc_id))
    sentences = sentences[:limit]
    text_of = {c.id: c for c in sentences}
    out = []
    for c in find_conflicts(sentences):
        a, b = text_of[c.claim_a], text_of[c.claim_b]
        out.append(ConflictReport(kind=c.kind, claim_a=a.text, source_a=titles[a.source], claim_b=b.text,
                                  source_b=titles[b.source], detail=c.detail))
    return out


is_usable_hit = _is_usable  # public alias for the preparation workflow
