
"""Sequential educational agent pipeline.

Research is deliberately bounded to the bank's existing approved knowledge.
No web crawling or automatic publication occurs in this milestone.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.learning import LearningAsset
from app.schemas.learning import LearningGenerateRequest
from app.services.retrieval import retrieve_hybrid, hit_to_dict


def _call_agent(role: str, instructions: str, payload: dict[str, Any]) -> str:
    settings = get_settings()

    if not settings.llm_api_key or not settings.llm_api_key.get_secret_value().strip():
        raise HTTPException(
            status_code=503,
            detail="AI agents are not configured. Set LLM_API_KEY.",
        )

    body = {
        "model": settings.llm_model,
        "temperature": 0.15,
        "messages": [
            {
                "role": "system",
                "content": f"You are EduForge {role}. {instructions}",
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
    }

    try:
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": (
                    f"Bearer {settings.llm_api_key.get_secret_value()}"
                ),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=settings.llm_timeout_seconds,
        )

        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]

        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict)
            )

        content = str(content).strip()

        if len(content) < 10:
            raise ValueError("Agent returned empty content")

        return content

    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{role} agent failed: {type(exc).__name__}",
        ) from exc


def run_educational_pipeline(
    db: Session,
    request: LearningGenerateRequest,
) -> dict:
    """Research -> analysis -> teacher authoring -> verification.

    Returns a review-only draft based on existing bank evidence.
    """

    query = " ".join(
        x
        for x in [
            request.subject,
            request.unit or "",
            request.lesson or "",
        ]
        if x
    )

    # Use hybrid retrieval instead of the old lexical-only retrieval.
    retrieval = retrieve_hybrid(db, query, limit=8)

    chunks = [
        hit_to_dict(hit)
        for hit in retrieval.hits
    ]

    approved_stmt = (
        select(LearningAsset)
        .where(
            LearningAsset.status == "APPROVED",
            LearningAsset.subject.ilike(f"%{request.subject}%"),
        )
        .order_by(LearningAsset.updated_at.desc())
        .limit(8)
    )

    approved_assets = list(db.scalars(approved_stmt).all())

    evidence = [
        {
            "kind": "document_chunk",
            "title": c["title"],
            "content": c["content"],
            "source_url": c.get("source_url"),
            "page_start": c.get("page_start"),
            "page_end": c.get("page_end"),
            "document_id": c["document_id"],
        }
        for c in chunks
    ]

    evidence.extend(
        {
            "kind": "approved_learning_asset",
            "title": a.title,
            "content": a.content,
            "asset_id": str(a.id),
            "sources": a.sources,
        }
        for a in approved_assets
    )

    curriculum = {
        "education_system": request.education_system,
        "stage": request.stage,
        "grade": request.grade,
        "subject": request.subject,
        "unit": request.unit,
        "lesson": request.lesson,
        "asset_type": request.asset_type,
        "language": request.language,
        "learner_level": request.learner_level,
        "learning_objectives": request.learning_objectives,
        "additional_instructions": request.additional_instructions,
    }

    research = _call_agent(
        "Research Agent",
        (
            "Use only the supplied bank evidence. Summarize relevant evidence, "
            "identify missing evidence, and explicitly say when the bank has "
            "no supporting source. Never invent sources or claim web search."
        ),
        {
            "curriculum_request": curriculum,
            "bank_evidence": evidence,
        },
    )

    analysis = _call_agent(
        "Analysis Agent",
        (
            "Break the teaching request into prerequisites, core concepts, "
            "misconceptions, and learning sequence. Separate evidence-backed "
            "facts from items needing verification."
        ),
        {
            "curriculum_request": curriculum,
            "research_report": research,
            "bank_evidence": evidence,
        },
    )

    lesson = _call_agent(
        "Teacher Agent",
        (
            "Create clear, age-appropriate educational content in the "
            "requested language. Teach step by step, define terms, show "
            "worked examples where relevant, and add practice questions "
            "with answers. Do not fabricate citations, facts, or page "
            "numbers. If evidence is insufficient, mark that clearly."
        ),
        {
            "curriculum_request": curriculum,
            "research_report": research,
            "analysis_plan": analysis,
            "bank_evidence": evidence,
        },
    )

    verification = _call_agent(
        "Verification Agent",
        (
            "Audit the draft for factual support against the supplied bank "
            "evidence, logical and age-level errors, ambiguous wording, and "
            "unsupported claims. Return a concise verdict with issues and "
            "corrections. Do not claim external verification."
        ),
        {
            "curriculum_request": curriculum,
            "draft": lesson,
            "bank_evidence": evidence,
            "research_report": research,
        },
    )

    return {
        "content": lesson,
        "agent_reports": {
            "research": research,
            "analysis": analysis,
            "verification": verification,
        },
        "evidence": evidence,
        "pipeline": [
            "research",
            "analysis",
            "teacher_authoring",
            "verification",
        ],
        "review_required": True,
        "publication_status": "DRAFT",
    }
    
