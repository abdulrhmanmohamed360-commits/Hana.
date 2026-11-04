from app.models.learning import LearningAsset
from app.schemas.learning import LearningAssetCreate, LearningPackRequest


def test_learning_asset_schema_defaults_to_arabic_draft():
    asset = LearningAssetCreate(subject="Mathematics", asset_type="explanation", title="Linear equations",
                                content="An equation states that two expressions are equal.")
    assert asset.language == "ar"
    assert asset.status == "DRAFT"
    assert asset.sources == []


def test_learning_asset_persists_curriculum_and_teaching_content(db_session):
    asset = LearningAsset(education_system="Egypt", stage="Preparatory", grade="Third Preparatory",
                          subject="Mathematics", unit="Algebra", lesson="Linear equations",
                          asset_type="explanation", title="Equation basics", content="شرح المعادلة",
                          language="ar", status="APPROVED", created_by="teacher")
    db_session.add(asset)
    db_session.commit()
    saved = db_session.get(LearningAsset, asset.id)
    assert saved.subject == "Mathematics"
    assert saved.grade == "Third Preparatory"
    assert saved.content == "شرح المعادلة"


def test_learning_pack_requires_subject_and_has_useful_defaults():
    request = LearningPackRequest(subject="Mathematics")
    assert request.include == ["explanation", "example", "exercise", "summary"]
    assert request.language == "ar"


def test_ai_authoring_requires_provider_configuration(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    from app.schemas.learning import LearningGenerateRequest
    from app.services import authoring

    monkeypatch.setattr(authoring, "get_settings", lambda: SimpleNamespace(llm_api_key=None))
    request = LearningGenerateRequest(subject="Mathematics", lesson="Fractions")
    with pytest.raises(HTTPException) as exc:
        authoring.generate_learning_content(request)
    assert exc.value.status_code == 503


def test_ai_authoring_uses_provider_and_returns_content(monkeypatch):
    from types import SimpleNamespace
    from pydantic import SecretStr
    from app.schemas.learning import LearningGenerateRequest
    from app.services import authoring

    monkeypatch.setattr(authoring, "get_settings", lambda: SimpleNamespace(
        llm_api_key=SecretStr("test-secret"), llm_base_url="https://llm.example/v1",
        llm_model="test-model", llm_timeout_seconds=10,
    ))

    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "شرح تعليمي واضح للمفهوم مع مثال محلول."}}]}

    def fake_post(url, **kwargs):
        assert url == "https://llm.example/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer test-secret"
        return FakeResponse()

    monkeypatch.setattr(authoring.httpx, "post", fake_post)
    request = LearningGenerateRequest(subject="Mathematics", lesson="Fractions")
    assert "مثال محلول" in authoring.generate_learning_content(request)
