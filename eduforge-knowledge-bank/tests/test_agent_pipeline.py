from app.schemas.learning import LearningGenerateRequest
from app.services import agent_pipeline


def test_agent_pipeline_runs_sequential_agents_and_returns_review_draft(db_session, monkeypatch):
    calls = []

    def fake_call(role, instructions, payload):
        calls.append(role)
        return f"{role} report with sufficient content."

    monkeypatch.setattr(agent_pipeline, "_call_agent", fake_call)
    request = LearningGenerateRequest(
        subject="Mathematics", grade="Third preparatory", stage="Preparatory",
        lesson="Linear equations", asset_type="explanation",
    )
    result = agent_pipeline.run_educational_pipeline(db_session, request)

    assert calls == ["Research Agent", "Analysis Agent", "Teacher Agent", "Verification Agent"]
    assert result["publication_status"] == "DRAFT"
    assert result["review_required"] is True
    assert len(result["agent_reports"]) == 3
    assert result["evidence"] == []



def test_agent_pipeline_endpoint_saves_only_draft(client, editor, monkeypatch):
    from app.api.v1 import learning as learning_api

    monkeypatch.setattr(learning_api, "run_educational_pipeline", lambda db, body: {
        "content": "شرح تعليمي تجريبي طويل بما يكفي للاختبار.",
        "agent_reports": {"research": "evidence", "analysis": "plan", "verification": "needs human review"},
        "evidence": [],
        "pipeline": ["research", "analysis", "teacher_authoring", "verification"],
        "review_required": True,
        "publication_status": "DRAFT",
    })
    response = client.post(
        "/api/v1/learning/agent-pipeline",
        headers=editor,
        json={"subject": "Mathematics", "grade": "Third preparatory",
              "stage": "Preparatory", "lesson": "Linear equations",
              "asset_type": "explanation", "language": "ar"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "DRAFT_REQUIRES_REVIEW"
    assert payload["asset"]["status"] == "DRAFT"
    assert payload["pipeline"][-1] == "verification"
