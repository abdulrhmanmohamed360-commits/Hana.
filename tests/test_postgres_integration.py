"""PostgreSQL + pgvector integration. SKIPPED unless TEST_DATABASE_URL points at an EMPTY database whose server has
the `vector` extension available, e.g.:
    TEST_DATABASE_URL=postgresql+psycopg://eduforge:eduforge@localhost:5432/eduforge_test pytest tests/test_postgres_integration.py
"""
import os

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set (needs PostgreSQL with pgvector)")


@pytest.fixture(scope="module")
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    os.environ["DATABASE_URL"] = URL
    from app.core.config import get_settings
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(URL)
    yield engine
    engine.dispose()


def test_migrations_create_search_indexes(pg):
    from sqlalchemy import text
    with pg.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes"))}
    assert {"ix_knowledge_chunks_fts", "ix_embeddings_hnsw_cosine", "ix_preparation_jobs_queue"} <= names


def test_hybrid_retrieval_uses_fts_and_pgvector(pg):
    from sqlalchemy.orm import Session
    from app.db.base import EMBEDDING_DIMENSIONS
    from app.models import Embedding
    from app.services.retrieval import retrieve_hybrid
    from tests.helpers import make_document
    with Session(pg) as db:
        _, chunk = make_document(db, "pg-doc", "المعادلة الخطية تحتوي على مجهول واحد")
        vec = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
        db.add(Embedding(owner_type="knowledge_chunk", owner_id=chunk.id, model="m", dimensions=EMBEDDING_DIMENSIONS, embedding=vec))
        db.commit()
        res = retrieve_hybrid(db, "المعادلة الخطية", query_embedding=vec, embedding_model="m")
        assert {"lexical", "fts", "vector"} <= set(res.signals_used)
