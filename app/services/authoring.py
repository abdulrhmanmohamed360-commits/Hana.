from __future__ import annotations

import json

import httpx
from fastapi import HTTPException

from app.core.config import get_settings
from app.schemas.learning import LearningGenerateRequest


def generate_learning_content(request: LearningGenerateRequest) -> str:
    """Generate a reviewable educational draft using an OpenAI-compatible API.

    This service deliberately does not publish or approve generated content.
    It also tells the model not to invent sources or claim external verification.
    """
    settings = get_settings()
    if not settings.llm_api_key or not settings.llm_api_key.get_secret_value().strip():
        raise HTTPException(status_code=503, detail="AI authoring is not configured. Set LLM_API_KEY.")

    curriculum = {
        "education_system": request.education_system, "stage": request.stage,
        "grade": request.grade, "subject": request.subject, "unit": request.unit,
        "lesson": request.lesson, "asset_type": request.asset_type,
        "language": request.language, "learner_level": request.learner_level,
        "learning_objectives": request.learning_objectives,
        "provided_source_references": request.source_references,
        "additional_instructions": request.additional_instructions,
    }
    system_prompt = (
        "You are EduForge, an expert educational author. Create accurate, age-appropriate, "
        "clear educational material in the requested language. Follow the requested asset type. "
        "For explanations, teach from prerequisites to the main idea, define terms, use a worked "
        "example where useful, and finish with a short recap. For exercises/quizzes, provide "
        "answers and concise explanations in a clearly separated answer section. Preserve "
        "mathematical notation. Do not invent citations, books, URLs, page numbers, or claims of "
        "having searched the web. Use only the supplied references; if none are supplied, state "
        "that references still need editorial verification. Return the educational content only."
    )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Prepare this educational asset:\n" + json.dumps(curriculum, ensure_ascii=False)},
        ],
    }
    base = settings.llm_base_url.rstrip("/")
    try:
        response = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}", "Content-Type": "application/json"},
            json=payload, timeout=settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        content = str(content).strip()
        if len(content) < 20:
            raise ValueError("Model returned empty or too-short content")
        return content
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"AI authoring provider failed: {type(exc).__name__}") from exc
