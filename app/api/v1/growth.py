from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, Role, enforce_rate_limit, get_db, require_role
from app.models.enums import TaskStatus
from app.models.ops import ResearchTask
from app.services.growth import create_research_tasks, execute_research_task, find_knowledge_gaps

router = APIRouter(prefix="/growth", tags=["knowledge-growth"], dependencies=[Depends(enforce_rate_limit)])


@router.get("/gaps")
def list_gaps(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db),
              _: Principal = Depends(require_role(Role.EDITOR))):
    gaps = find_knowledge_gaps(db, limit=limit)
    return {"count": len(gaps), "required_asset_types": ["explanation", "example", "exercise", "quiz", "summary", "glossary"], "gaps": gaps}


@router.post("/scan", status_code=201)
def scan_and_queue(db: Session = Depends(get_db), who: Principal = Depends(require_role(Role.EDITOR))):
    tasks = create_research_tasks(db, actor=who.actor)
    return {"created_tasks": len(tasks), "tasks": [{"id": str(t.id), "goal": t.goal, "status": t.status.value} for t in tasks],
            "note": "Tasks are queued persistently; a worker or explicit run call is required to execute them."}


@router.get("/tasks")
def list_research_tasks(status: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                       db: Session = Depends(get_db), _: Principal = Depends(require_role(Role.EDITOR))):
    stmt = select(ResearchTask).order_by(ResearchTask.created_at.desc()).limit(limit)
    if status:
        try:
            task_status = TaskStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid task status") from exc
        stmt = stmt.where(ResearchTask.status == task_status)
    rows = db.scalars(stmt).all()
    return [{"id": str(t.id), "goal": t.goal, "status": t.status.value,
             "attempts": t.attempts, "meta": t.meta, "last_error": t.last_error} for t in rows]


@router.get("/tasks/{task_id}")
def get_research_task(task_id: uuid.UUID, db: Session = Depends(get_db),
                      _: Principal = Depends(require_role(Role.EDITOR))):
    task = db.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    return {"id": str(task.id), "goal": task.goal, "status": task.status.value, "attempts": task.attempts,
            "max_attempts": task.max_attempts, "last_error": task.last_error, "created_at": task.created_at,
            "started_at": task.started_at, "finished_at": task.finished_at,
            "result_count": (task.meta or {}).get("result_count"),
            "ingested_count": (task.meta or {}).get("ingested_count"),
            "results_are_unverified": True}


@router.post("/tasks/{task_id}/research")
def run_research(task_id: uuid.UUID, db: Session = Depends(get_db),
                 _: Principal = Depends(require_role(Role.EDITOR))):
    task = db.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    if task.status == TaskStatus.SUCCEEDED:
        return {"task_id": str(task.id), "status": task.status.value,
                "results": (task.meta or {}).get("search_results", []), "publication_allowed": False}
    return execute_research_task(db, task)
