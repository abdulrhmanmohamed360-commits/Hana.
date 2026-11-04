"""Knowledge preparation workflow (POST /knowledge/prepare).

retrieve approved knowledge -> identify gaps -> (optional research) -> generate -> verify -> structured package.
Nothing generated here is ever APPROVED; approval stays an admin action on the existing /learning endpoints.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.llm import AgentOutputError, StructuredLLM
from app.agents.schemas import (AnalysisPlan, EvidenceRef, ResearchReport, ReviewVerdict, TeacherDraft)
from app.engines import math_gen
from app.engines.claims import ClaimCandidate, candidates_for_item, classify_sentence, split_sentences
from app.engines.text_ar import normalize_arabic
from app.models import ExtractedClaim, EvidenceRecord, LearningAsset
from app.models.enums import TaskStatus
from app.schemas.prepare import (STATUS_SEVERITY, ClaimReport, GapReport, PackageItem, PreparePackage,
                                 PrepareRequest, Provenance, RejectedItem, RetrievalReport)
from app.core.errors import InvalidInputError, NotFoundError
from app.services import audit, coverage, graph
from app.services import curriculum as cur_svc
from app.services.embeddings import Embedder, EmbeddingError
from app.services.retrieval import HybridResult, retrieve_hybrid
from app.services.verification import conflicts_between_sources, is_usable_hit, verify_and_record, worst_status

ASSET_TO_CONTENT = {"explanation": "explanation", "definition": "definitions", "glossary": "definitions",
                    "formula": "formulas", "example": "solved_examples", "exercise": "exercises",
                    "quiz": "questions", "summary": "summary"}
GROUP_ORDER = ["explanation", "definitions", "formulas", "solved_examples", "exercises", "questions", "summary"]
ASSET_TYPE_FOR = {"explanation": "explanation", "definitions": "definition", "formulas": "formula",
                  "solved_examples": "example", "exercises": "exercise", "questions": "quiz", "summary": "summary"}
_SUBJECT_ALIASES = {"math": "mathematics", "maths": "mathematics", "رياضيات": "mathematics", "الرياضيات": "mathematics"}
NOTICE = ("Items with trust DRAFT or SOURCE_MATERIAL are NOT approved educational content. verification_status states "
          "exactly what was checked: MATH_VERIFIED = deterministic SymPy check; SOURCE_SUPPORTED = lexical support from "
          "a reviewed source (not proof); UNVERIFIED/CONFLICTED/REJECTED need human review. Only an admin can approve "
          "assets (POST /api/v1/learning/assets/{id}/approve).")


# ------------------------------------------------------------------ context / helpers
@dataclass
class PrepareContext:
    actor: str
    llm: StructuredLLM | None = None
    embedder: Embedder | None = None
    allow_llm: bool = False                      # async jobs only; sync requests never call the network
    research_runner: Callable[[Session, object], dict] | None = None
    progress: Callable[[str, int, str], None] = lambda stage, pct, note: None
    log: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)
    node_id: uuid.UUID | None = None            # resolved curriculum lesson node, when the request maps to one


@dataclass
class Group:
    """A set of same-type items produced together; persisted as ONE draft LearningAsset."""
    content_type: str
    origin: str
    title: str
    items: list[dict]                            # {"ref","title","body","data"}
    provenance: list[Provenance]


def _canon(text: str | None) -> str:
    c = normalize_arabic(text or "").casefold().strip()
    return _SUBJECT_ALIASES.get(c, c)


def _same(a: str | None, b: str | None) -> bool:
    ca, cb = _canon(a), _canon(b)
    return bool(ca) and bool(cb) and (ca == cb or (min(len(ca), len(cb)) >= 4 and (ca in cb or cb in ca)))


def prepare_key(req: PrepareRequest) -> str:
    parts = [req.country, req.curriculum, req.grade, str(req.semester or ""), req.subject, req.unit or "",
             req.lesson, req.language, req.difficulty]
    return hashlib.sha256("|".join(_canon(p) for p in parts).encode()).hexdigest()[:40]


def request_hash(req: PrepareRequest) -> str:
    material = prepare_key(req) + f"|{req.curriculum_id}|" + ",".join(sorted(req.requested_content)) + f"|{req.mode}|{req.allow_generation}|{req.allow_research}"
    return hashlib.sha256(material.encode()).hexdigest()


def _prov_from_sources(sources: list) -> list[Provenance]:
    out = []
    for d in sources or []:
        if isinstance(d, dict):
            out.append(Provenance(kind="source_passage" if d.get("kind") == "document_chunk" else "approved_asset",
                                  ref_id=str(d.get("document_id") or d.get("asset_id") or "") or None,
                                  title=str(d.get("title") or d.get("url") or d.get("source_url") or "source")[:300],
                                  source_url=d.get("source_url") or d.get("url"), page_start=d.get("page_start"),
                                  page_end=d.get("page_end")))
    return out


# ------------------------------------------------------------------ existing knowledge
def find_approved_assets(db: Session, req: PrepareRequest, node_id: uuid.UUID | None = None) -> list[LearningAsset]:
    rows = db.scalars(select(LearningAsset).where(LearningAsset.status == "APPROVED",
                                                  LearningAsset.language == req.language).limit(5000)).all()
    out = []
    for a in rows:
        if node_id is not None and a.curriculum_node_id == node_id:     # explicit curriculum mapping wins over text matching
            out.append(a)
            continue
        if not (_same(a.education_system, req.country) and _same(a.grade, req.grade)
                and _same(a.subject, req.subject) and _same(a.lesson, req.lesson)):
            continue
        sem = (a.meta or {}).get("semester")
        if sem and req.semester and int(sem) != req.semester:
            continue
        out.append(a)
    return out


def _approved_item(a: LearningAsset, ctype: str) -> PackageItem:
    return PackageItem(item_id=str(a.id), content_type=ctype, title=a.title, body=a.content,
                       data=(a.meta or {}).get("structured"), language=a.language, origin="approved_asset",
                       trust="APPROVED", verification_status="APPROVED",
                       provenance=[Provenance(kind="approved_asset", ref_id=str(a.id), title=a.title,
                                              verification_status="APPROVED",
                                              note=f"approved by {a.reviewed_by or 'reviewer'}")] + _prov_from_sources(a.sources))


def find_drafts(db: Session, req: PrepareRequest, key: str) -> dict[str, LearningAsset]:
    rows = db.scalars(select(LearningAsset).where(LearningAsset.status.in_(["DRAFT", "REVIEW"]),
                                                  LearningAsset.language == req.language,
                                                  LearningAsset.education_system == req.country)
                      .order_by(LearningAsset.created_at.desc()).limit(2000)).all()
    found: dict[str, LearningAsset] = {}
    for a in rows:
        m = a.meta or {}
        if m.get("prepare_key") == key and m.get("content_type") and m["content_type"] not in found:
            found[m["content_type"]] = a
    return found


def _items_from_asset(db: Session, a: LearningAsset, with_answers: bool) -> list[PackageItem]:
    """Rebuild package items for a stored draft, using the claim verdicts persisted when it was created."""
    claims = db.scalars(select(ExtractedClaim).where(ExtractedClaim.learning_asset_id == a.id)).all()
    counts = dict(db.execute(select(EvidenceRecord.claim_id, func.count()).where(
        EvidenceRecord.claim_id.in_([c.id for c in claims] or [None])).group_by(EvidenceRecord.claim_id)).all()) if claims else {}
    by_ref: dict[str, list[ClaimReport]] = {}
    for c in claims:
        m = c.meta or {}
        by_ref.setdefault(m.get("item_ref", ""), []).append(ClaimReport(
            claim_id=str(c.id), text=c.claim_text, claim_type=c.claim_type.value,
            verification_status=m.get("verification_status", "UNVERIFIED"), method=m.get("method", ""),
            detail=m.get("detail", ""), evidence_count=int(counts.get(c.id, 0))))
    meta, out = a.meta or {}, []
    for it in (meta.get("structured") or {}).get("items", []):
        reports = by_ref.get(it["ref"], [])
        out.append(PackageItem(
            item_id=f"{a.id}#{it['ref']}", content_type=meta["content_type"], title=it.get("title") or a.title,
            body=it["body"], data=_visible(meta["content_type"], it.get("data"), with_answers), language=a.language,
            origin=meta.get("origin", "agent_draft"), trust="DRAFT",
            verification_status=worst_status([r.verification_status for r in reports]), claims=reports,
            provenance=[Provenance(**p) for p in meta.get("provenance", [])]))
    return out


def _visible(ctype: str, data: dict | None, with_answers: bool) -> dict | None:
    """When 'answers' was not requested, do not leak model answers inside exercise/question data."""
    if not data or with_answers or ctype not in ("exercises", "questions"):
        return data
    hidden = {"answer", "steps", "correct_label"}
    return {k: v for k, v in data.items() if k not in hidden}


# ------------------------------------------------------------------ retrieval
def retrieve_evidence(db: Session, req: PrepareRequest, ctx: PrepareContext) -> HybridResult:
    query = " ".join(x for x in (req.lesson, req.subject, req.unit or "") if x)
    vec, model = None, None
    if ctx.embedder is not None:
        try:
            vec, model = ctx.embedder.embed([normalize_arabic(query)])[0], ctx.embedder.model
        except EmbeddingError as exc:
            ctx.log.append(f"semantic signal skipped: {exc}")
    return retrieve_hybrid(db, query, limit=get_limit(), query_embedding=vec, embedding_model=model)


def get_limit() -> int:
    from app.core.config import get_settings
    return get_settings().prepare_max_evidence_chunks


def _passage_prov(hit) -> Provenance:
    return Provenance(kind="source_passage", ref_id=str(hit.chunk.id), title=hit.document.title or "Untitled source",
                      source_url=hit.document.original_url, page_start=hit.chunk.page_start,
                      page_end=hit.chunk.page_end)


def source_items(db: Session, hits, wanted: set[str]) -> dict[str, list[PackageItem]]:
    """Extractive items taken verbatim from retrieved passages (no generation, no paraphrase)."""
    out: dict[str, list[PackageItem]] = {}
    for hit in hits:
        usable = is_usable_hit(db, hit)
        status = "SOURCE_SUPPORTED" if usable else "UNVERIFIED"
        prov = _passage_prov(hit)
        prov.verification_status = status
        prov.note = None if usable else "unreviewed or non-allowlisted source content"
        if "explanation" in wanted and len(out.get("explanation", [])) < 3:
            out.setdefault("explanation", []).append(PackageItem(
                item_id=str(hit.chunk.id), content_type="explanation", title=hit.document.title or "Source passage",
                body=hit.chunk.content[:1500], language=hit.chunk.language, origin="source_passage",
                trust="SOURCE_MATERIAL", verification_status=status, provenance=[prov]))
        if "definitions" in wanted:
            for n, sentence in enumerate(split_sentences(hit.chunk.content, min_len=20)):
                if classify_sentence(sentence) == "DEFINITION" and len(out.get("definitions", [])) < 6:
                    out.setdefault("definitions", []).append(PackageItem(
                        item_id=f"{hit.chunk.id}#s{n}", content_type="definitions", title=sentence[:80], body=sentence,
                        language=hit.chunk.language, origin="source_passage", trust="SOURCE_MATERIAL",
                        verification_status=status, provenance=[prov]))
    return out


# ------------------------------------------------------------------ generation
def math_groups(req: PrepareRequest, key: str, needed: set[str]) -> list[Group]:
    gen = math_gen.generate(key, req.difficulty, req.language)
    ar = req.language.startswith("ar")
    prov = [Provenance(kind="generator", ref_id="linear_equations_v1", title="EduForge deterministic linear-equation generator",
                       note="values computed with exact integer arithmetic; independently re-verified with SymPy")]
    groups = []
    spec = {"solved_examples": ("أمثلة محلولة" if ar else "Solved examples", math_gen.render_solved_example),
            "exercises": ("تمارين" if ar else "Exercises", lambda d, a: math_gen.render_exercise(d, a)),
            "questions": ("أسئلة اختيار من متعدد" if ar else "Multiple-choice questions", math_gen.render_mcq)}
    for ctype in ("solved_examples", "exercises", "questions"):
        if ctype in needed:
            title, render = spec[ctype]
            items = [{"ref": f"{ctype}-{n}", "title": d.get("title") or f"{title} {n + 1}", "body": render(d, ar), "data": d}
                     for n, d in enumerate(gen[ctype])]
            groups.append(Group(ctype, "deterministic_generator", f"{req.lesson} - {title}", items, prov))
    return groups


def _evidence_refs(hits) -> list[EvidenceRef]:
    return [EvidenceRef(evidence_id=f"E{i}", title=h.document.title or "Untitled", content=h.chunk.content[:1800],
                        chunk_id=str(h.chunk.id), document_id=str(h.document.id), source_url=h.document.original_url,
                        page_start=h.chunk.page_start, page_end=h.chunk.page_end,
                        verification_status=(h.chunk.meta or {}).get("verification_status", "SOURCE_MATERIAL"))
            for i, h in enumerate(hits, 1)]


def agent_groups(db: Session, req: PrepareRequest, ctx: PrepareContext, hits, needed: set[str]) -> list[Group]:
    """Research -> Analysis -> Teacher -> Review, every hop validated against a Pydantic schema."""
    llm = ctx.llm
    evidence = _evidence_refs(hits)
    valid_ids = {e.evidence_id for e in evidence}
    request_payload = {"country": req.country, "curriculum": req.curriculum, "grade": req.grade, "semester": req.semester,
                       "subject": req.subject, "unit": req.unit, "lesson": req.lesson, "language": req.language,
                       "difficulty": req.difficulty, "content_needed": sorted(needed)}
    ev_payload = [e.model_dump() for e in evidence]
    research = llm.run(db, "Research Agent", "Summarize only what the evidence says about the lesson; list which of the "
                       "needed content types the evidence cannot support.", {"request": request_payload, "evidence": ev_payload},
                       ResearchReport)
    analysis = llm.run(db, "Analysis Agent", "Identify prerequisites, core concepts, common misconceptions and a teaching "
                       "sequence for this grade.", {"request": request_payload, "research": research.model_dump(),
                                                   "evidence": ev_payload}, AnalysisPlan)
    teacher = llm.run(db, "Teacher Agent",
                      "Write the needed content in the requested language and level. Fill only fields for content types "
                      "listed in content_needed. Solved examples/exercises must be linear equations with exact answers; "
                      "they will be re-checked by a computer algebra system and wrong ones discarded.",
                      {"request": request_payload, "research": research.model_dump(), "analysis": analysis.model_dump(),
                       "evidence": ev_payload}, TeacherDraft)
    verdict = llm.run(db, "Verification Agent", "Review the draft for unsupported claims and age-inappropriate wording. "
                      "Your verdict is advisory and cannot approve anything.",
                      {"request": request_payload, "draft": teacher.model_dump(), "evidence": ev_payload}, ReviewVerdict)
    ctx.advisory += [f"[{i.severity}] {i.problem} — {i.claim[:120]}" for i in verdict.issues]
    by_id = {e.evidence_id: e for e in evidence}

    def prov(ids: list[str]) -> tuple[list[Provenance], int]:
        good = [i for i in dict.fromkeys(ids) if i in valid_ids]
        ps = [Provenance(kind="source_passage", ref_id=by_id[i].chunk_id, title=by_id[i].title,
                         source_url=by_id[i].source_url, page_start=by_id[i].page_start, page_end=by_id[i].page_end)
              for i in good]
        return ps, len(ids) - len(good)

    base_prov = [Provenance(kind="agent_draft", ref_id=None, title="Teacher Agent draft", note="model-written; unverified")]
    groups: list[Group] = []
    dropped = 0
    if "explanation" in needed and teacher.explanation:
        ps, bad = prov(teacher.evidence_ids_used)
        dropped += bad
        groups.append(Group("explanation", "agent_draft", f"{req.lesson} - explanation",
                            [{"ref": "explanation-0", "title": req.lesson, "body": teacher.explanation, "data": None}],
                            base_prov + ps))
    if "definitions" in needed and teacher.definitions:
        items, ps_all = [], []
        for n, d in enumerate(teacher.definitions):
            ps, bad = prov(d.evidence_ids)
            dropped += bad
            ps_all += ps
            items.append({"ref": f"definitions-{n}", "title": d.term, "body": f"{d.term}: {d.definition}", "data": None})
        groups.append(Group("definitions", "agent_draft", f"{req.lesson} - definitions", items, base_prov + ps_all))
    if "formulas" in needed and teacher.formulas:
        groups.append(Group("formulas", "agent_draft", f"{req.lesson} - formulas",
                            [{"ref": f"formulas-{n}", "title": f"{n + 1}", "body": f, "data": None} for n, f in enumerate(teacher.formulas)],
                            base_prov))
    if "summary" in needed and teacher.summary:
        groups.append(Group("summary", "agent_draft", f"{req.lesson} - summary",
                            [{"ref": "summary-0", "title": req.lesson, "body": teacher.summary, "data": None}], base_prov))
    ar = req.language.startswith("ar")
    if "solved_examples" in needed and teacher.solved_examples:
        items = [{"ref": f"solved_examples-{n}", "title": e.title or f"{n + 1}",
                  "body": math_gen.render_solved_example({**e.model_dump(), "title": e.title or f"{n + 1}"}, ar),
                  "data": e.model_dump()} for n, e in enumerate(teacher.solved_examples)]
        groups.append(Group("solved_examples", "agent_draft", f"{req.lesson} - solved examples", items, base_prov))
    if "exercises" in needed and teacher.exercises:
        items = [{"ref": f"exercises-{n}", "title": f"{n + 1}", "body": math_gen.render_exercise(e.model_dump(), ar),
                  "data": e.model_dump()} for n, e in enumerate(teacher.exercises)]
        groups.append(Group("exercises", "agent_draft", f"{req.lesson} - exercises", items, base_prov))
    if dropped:
        ctx.advisory.append(f"{dropped} evidence citation(s) by the Teacher Agent referenced unknown evidence ids and were dropped")
    return groups


# ------------------------------------------------------------------ persistence + verification
def persist_and_verify(db: Session, req: PrepareRequest, key: str, group: Group, ctx: PrepareContext,
                       with_answers: bool) -> tuple[list[PackageItem], list[RejectedItem]]:
    asset = LearningAsset(
        education_system=req.country, stage="unspecified", grade=req.grade, subject=req.subject, unit=req.unit,
        lesson=req.lesson, asset_type=ASSET_TYPE_FOR[group.content_type], title=group.title[:300],
        content="\n\n".join(i["body"] for i in group.items), language=req.language, status="DRAFT", quality_score=0.0,
        sources=[p.model_dump() for p in group.provenance], learning_objectives=[],
        tags=["prepare-generated", "needs-review", group.origin], created_by=ctx.actor,
        meta={"prepare_key": key, "content_type": group.content_type, "origin": group.origin,
              "curriculum": req.curriculum, "semester": req.semester, "difficulty": req.difficulty,
              "structured": {"items": group.items}, "provenance": [p.model_dump() for p in group.provenance],
              "review_required": True})
    db.add(asset)
    db.flush()
    if ctx.node_id is not None:
        try:
            graph.link_asset(db, asset, ctx.node_id, actor=ctx.actor, origin="PREPARE")
        except (InvalidInputError, NotFoundError) as exc:
            ctx.log.append(f"asset {asset.id} not linked to the curriculum: {exc}")
    candidates: list[ClaimCandidate] = []
    for it in group.items:
        candidates += candidates_for_item(group.content_type, it.get("data"), it["body"], it["ref"])
    reports = verify_and_record(db, candidates, asset_id=asset.id, actor=ctx.actor)
    by_ref: dict[str, list[ClaimReport]] = {}
    for cand, rep in zip(candidates, reports):
        by_ref.setdefault(cand.item_ref, []).append(rep)
    kept, accepted, rejected = [], [], []
    for it in group.items:
        reps = by_ref.get(it["ref"], [])
        status = worst_status([r.verification_status for r in reps])
        if status == "REJECTED":
            rejected.append(RejectedItem(content_type=group.content_type, title=it["title"],
                                         reasons=[r.detail for r in reps if r.verification_status == "REJECTED"]))
            continue
        accepted.append(it)
        kept.append(PackageItem(
            item_id=f"{asset.id}#{it['ref']}", content_type=group.content_type, title=it["title"], body=it["body"],
            data=_visible(group.content_type, it.get("data"), with_answers), language=req.language, origin=group.origin,
            trust="DRAFT", verification_status=status, claims=reps, provenance=group.provenance))
    meta = dict(asset.meta)
    meta["structured"] = {"items": accepted}
    meta["rejected_items"] = [r.model_dump() for r in rejected]
    asset.meta = meta
    asset.content = "\n\n".join(i["body"] for i in accepted) or asset.content
    if not accepted:
        asset.status = "REJECTED"
        asset.tags = [*asset.tags, "all-items-rejected"]
    audit.record(db, actor=ctx.actor, action="prepare.draft_created", entity_type="learning_asset", entity_id=asset.id,
                 details={"content_type": group.content_type, "accepted": len(accepted), "rejected": len(rejected)})
    db.commit()
    return kept, rejected


# ------------------------------------------------------------------ optional external research
def research_missing(db: Session, req: PrepareRequest, key: str, missing: list[str], ctx: PrepareContext) -> str | None:
    """Queue (idempotently) and run one bounded research task. Returns an error reason or None on success."""
    from app.models.ops import ResearchTask
    idem = f"prepare-research:{key}"
    task = db.scalar(select(ResearchTask).where(ResearchTask.idempotency_key == idem))
    if task is not None and task.status == TaskStatus.SUCCEEDED:
        ctx.log.append("research task already completed earlier; reusing its (unverified) results")
        return None
    if task is None:
        task = ResearchTask(goal=f"Find reliable educational sources for {req.subject} / {req.grade} / {req.lesson}; "
                                 f"prepare: {', '.join(missing)}", created_by=ctx.actor, idempotency_key=idem,
                            status=TaskStatus.PENDING, priority=3,
                            meta={"origin": "prepare", "prepare_key": key, "publication_requires_review": True})
        db.add(task)
        db.commit()
    runner = ctx.research_runner
    if runner is None:
        from app.services.growth import execute_research_task as runner
    try:
        result = runner(db, task)
        ctx.log.append(f"research task {task.id}: {result.get('result_count', 0)} lead(s); fetched pages are UNVERIFIED")
        return None
    except Exception as exc:  # HTTPException(503) when Tavily is not configured, network errors, ...
        detail = getattr(exc, "detail", None) or type(exc).__name__
        return f"research unavailable: {str(detail)[:200]}"


# ------------------------------------------------------------------ orchestration
def build_package(db: Session, req: PrepareRequest, ctx: PrepareContext) -> PreparePackage:
    key = prepare_key(req)
    wanted = list(req.requested_content)
    with_answers = "answers" in wanted
    items: dict[str, list[PackageItem]] = {t: [] for t in wanted}
    rejected: list[RejectedItem] = []
    gap_reasons: dict[str, str] = {}
    log = ctx.log

    # 1. existing approved knowledge
    ctx.progress("retrieving", 10, "approved knowledge and source passages")
    resolved = cur_svc.resolve_context(db, country=req.country, grade=req.grade, subject=req.subject, lesson=req.lesson,
                                       term=req.semester, unit=req.unit, curriculum_id=req.curriculum_id)
    if resolved.status == "RESOLVED":
        ctx.node_id = resolved.node.id
        log.append(f"curriculum context resolved: {resolved.curriculum.name} v{resolved.curriculum.version} > lesson '{resolved.node.name}'")
    else:
        log.append(f"curriculum context {resolved.status}: {resolved.reason or 'no mapping'}")
    approved = find_approved_assets(db, req, ctx.node_id)
    for a in approved:
        ctype = ASSET_TO_CONTENT.get(a.asset_type)
        if ctype in wanted:
            items[ctype].append(_approved_item(a, ctype))
    log.append(f"found {len(approved)} approved asset(s) matching the curriculum")
    result = retrieve_evidence(db, req, ctx)
    hits = result.hits
    log.append(f"retrieved {len(hits)} passage(s) via {'+'.join(result.signals_used) or 'no signal'}")

    # 2. gaps; reuse earlier drafts instead of regenerating
    ctx.progress("planning", 25, "identifying missing content")
    drafts = find_drafts(db, req, key)
    for ctype in GROUP_ORDER:
        if ctype in wanted and not items[ctype] and ctype in drafts:
            reused = _items_from_asset(db, drafts[ctype], with_answers)
            if reused:
                items[ctype] += reused
                log.append(f"reused earlier draft for {ctype} (verification verdicts restored from stored claims)")
    needed = {t for t in GROUP_ORDER if t in wanted and not items[t]}

    # 3. optional research (async only) when content still has no source basis
    if req.allow_research and needed:
        ctx.progress("researching", 40, "external research on approved sources")
        err = research_missing(db, req, key, sorted(needed), ctx)
        if err:
            for t in needed:
                gap_reasons[t] = err
        else:
            result = retrieve_evidence(db, req, ctx)
            hits = result.hits
            log.append(f"re-retrieved after research: {len(hits)} passage(s) (new web content is UNVERIFIED)")

    # 4. generation
    ctx.progress("generating", 60, "deterministic math generator and agents")
    if req.allow_generation and needed:
        math_needed = needed & {"solved_examples", "exercises", "questions"}
        groups: list[Group] = []
        if math_needed and math_gen.supports_lesson(req.subject, req.lesson):
            groups += math_groups(req, key, math_needed)
            log.append("generated linear-equation items with the deterministic generator (re-verified below)")
        if ctx.allow_llm and ctx.llm is not None:
            remaining = needed - {g.content_type for g in groups}
            if remaining:
                try:
                    groups += agent_groups(db, req, ctx, hits, remaining)
                    log.append("ran Research, Analysis, Teacher and Verification agents with schema-validated output")
                except AgentOutputError as exc:
                    log.append(f"agent pipeline failed: {exc}")
                    for t in remaining:
                        gap_reasons.setdefault(t, f"agent pipeline failed: {str(exc)[:160]}")
        ctx.progress("verifying", 80, "claim extraction and verification")
        for group in groups:
            kept, rej = persist_and_verify(db, req, key, group, ctx, with_answers)
            items[group.content_type] += kept
            rejected += rej
        # extractive fallback from retrieved passages for what is still missing
        still = {t for t in needed if not items[t]} & {"explanation", "definitions"}
        if still and hits:
            for t, found in source_items(db, hits, still).items():
                items[t] += found
            log.append("filled explanation/definitions extractively from retrieved passages (no paraphrase)")
    elif needed:
        for t in needed:
            gap_reasons.setdefault(t, "generation disabled by request (allow_generation=false)")
        extractive = source_items(db, hits, needed & {"explanation", "definitions"}) if hits else {}
        for t, found in extractive.items():
            items[t] += found

    # 5. derived views: answers + sources
    ctx.progress("assembling", 95, "building structured package")
    if "answers" in wanted:
        for src in (*items.get("exercises", []), *items.get("questions", [])):
            d = src.data or {}
            if src.origin == "approved_asset" or not d:
                continue
            ans = d.get("answer") or next((o["value"] for o in d.get("options", []) if o["label"] == d.get("correct_label")), None)
            if ans is not None:
                items["answers"].append(PackageItem(
                    item_id=f"{src.item_id}:answer", content_type="answers", title=src.title,
                    body=f"{d.get('variable', '')} = {ans}", data={"for_item": src.item_id, "answer": str(ans),
                                                                      "steps": d.get("steps", [])},
                    language=src.language, origin=src.origin, trust=src.trust,
                    verification_status=src.verification_status, claims=src.claims, provenance=src.provenance))
    if "sources" in wanted:
        seen: set[tuple] = set()
        snapshot = [it for t, lst in items.items() if t != "sources" for it in lst]
        if True:
            for it in snapshot:
                for p in it.provenance:
                    k = (p.kind, p.ref_id, p.title)
                    if k not in seen:
                        seen.add(k)
                        items["sources"].append(PackageItem(
                            item_id=f"src:{len(seen)}", content_type="sources", title=p.title, body=p.source_url or p.title,
                            data=p.model_dump(), language=req.language, origin="source_passage" if p.kind == "source_passage" else
                            ("approved_asset" if p.kind == "approved_asset" else "deterministic_generator"),
                            trust="APPROVED" if p.kind == "approved_asset" else "SOURCE_MATERIAL",
                            verification_status="APPROVED" if p.kind == "approved_asset" else
                            (p.verification_status if p.verification_status in STATUS_SEVERITY else "UNVERIFIED"),
                            provenance=[p]))

    missing = []
    for t in wanted:
        if not items[t]:
            reason = gap_reasons.get(t)
            if t == "sources":
                reason = "no provenance available because no content was produced"
            elif t == "answers":
                reason = reason or "no machine-readable exercises/questions to derive answers from"
            elif reason is None:
                if req.mode == "sync":
                    reason = "not in the knowledge base; generation without a network needs a deterministic generator " \
                             "for this lesson — retry with mode='async' and an LLM provider configured"
                elif ctx.llm is None:
                    reason = "not in the knowledge base and no LLM provider is configured (LLM_API_KEY)"
                else:
                    reason = "the agents produced no usable content for this type"
            missing.append(GapReport(content_type=t, reason=reason))
    total_items = sum(len(v) for t, v in items.items() if t != "sources")
    summary: dict[str, int] = {}
    for lst in items.values():
        for it in lst:
            summary[it.verification_status] = summary.get(it.verification_status, 0) + 1
    status = "NOT_READY" if total_items == 0 else ("COMPLETE" if not missing else "PARTIAL")
    conflicts = conflicts_between_sources(hits)
    audit.record(db, actor=ctx.actor, action="prepare.package", entity_type="prepare", entity_id=key,
                 details={"status": status, "missing": [m.content_type for m in missing], "trust_summary": summary})
    db.commit()
    cur_block = state = None
    if ctx.node_id is not None:
        node = resolved.node
        cur_block = {"status": "RESOLVED", "curriculum_id": str(resolved.curriculum.id), "curriculum": resolved.curriculum.name,
                     "version": resolved.curriculum.version, "path": {k.lower(): v.name for k, v in resolved.path.items()},
                     **cur_svc.lesson_context(db, node)}
        state = coverage.lesson_state(db, node, resolved.path["SUBJECT"].name if "SUBJECT" in resolved.path else req.subject)
    else:
        cur_block = {"status": resolved.status, "reason": resolved.reason, "candidates": resolved.candidates}
    return PreparePackage(
        request=req, status=status, generated_at=datetime.now(timezone.utc), items=items, missing=missing,
        rejected=rejected, conflicts=conflicts, trust_summary=summary,
        contains_unapproved_content=any(i.trust != "APPROVED" for lst in items.values() for i in lst),
        retrieval=RetrievalReport(mode=result.mode, signals_used=result.signals_used,
                                  signals_unavailable=result.signals_unavailable, passages_considered=len(hits)),
        workflow=list(log), advisory_notes=list(ctx.advisory), curriculum=cur_block, knowledge_state=state, notice=NOTICE)
