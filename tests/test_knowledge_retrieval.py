from app.models import KnowledgeChunk, RawDocument
from app.models.enums import DocumentStatus
from app.services.ingestion import content_hash
from app.services.retrieval import retrieve_chunks


def test_retrieval_returns_matching_chunk_with_provenance(db_session):
    text = "Photosynthesis is the process by which green plants convert light energy into chemical energy."
    doc = RawDocument(title="Biology textbook", content_hash=content_hash(text), text_content=text,
                      status=DocumentStatus.EXTRACTED, meta={})
    db_session.add(doc)
    db_session.flush()
    chunk = KnowledgeChunk(document_id=doc.id, chunk_index=0, page_start=12, page_end=12,
                           content=text, content_hash=content_hash(text), language="en", token_estimate=15, meta={})
    db_session.add(chunk)
    db_session.commit()

    results = retrieve_chunks(db_session, "photosynthesis green plants", limit=5)
    assert len(results) == 1
    assert results[0]["title"] == "Biology textbook"
    assert results[0]["page_start"] == 12
    assert "light energy" in results[0]["content"]


def test_knowledge_retrieve_api_returns_passages(client, reader, db_session):
    text = "The water cycle describes evaporation, condensation, and precipitation."
    doc = RawDocument(title="Earth science", content_hash=content_hash(text), text_content=text,
                      status=DocumentStatus.EXTRACTED, meta={})
    db_session.add(doc)
    db_session.flush()
    db_session.add(KnowledgeChunk(document_id=doc.id, chunk_index=0, page_start=4, page_end=4,
                                  content=text, content_hash=content_hash(text), language="en",
                                  token_estimate=10, meta={}))
    db_session.commit()
    response = client.post("/api/v1/knowledge/retrieve", json={"query": "water cycle evaporation"}, headers=reader)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["retrieval_mode"] == "lexical_baseline"
    assert payload["count"] == 1
    assert payload["results"][0]["page_start"] == 4
