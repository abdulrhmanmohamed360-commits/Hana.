"""Hybrid retrieval, claim verification for generated content, persistent preparation jobs.

IMPORTANT: 0001 builds tables from the *current* model metadata, so on a fresh database these objects already exist.
Every step therefore checks the live schema first and is a no-op when the object is present.

* knowledge_chunks.content_norm  (+ backfill) and a GIN full-text index (PostgreSQL)
* HNSW cosine index on embeddings.embedding (PostgreSQL / pgvector >= 0.5)
* extracted_claims.raw_document_id becomes nullable; new learning_asset_id FK; has_origin check
* preparation_jobs table
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from app.engines.text_ar import normalize_arabic

revision = "0005_verification_hybrid_prepare"
down_revision = "0004_learning_assets"
branch_labels = None
depends_on = None


def _cols(insp, table):
    return {c["name"] for c in insp.get_columns(table)} if table in insp.get_table_names() else set()


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    pg = bind.dialect.name == "postgresql"
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    # --- chunks: normalized text for lexical + FTS
    if "content_norm" not in _cols(insp, "knowledge_chunks"):
        op.add_column("knowledge_chunks", sa.Column("content_norm", sa.Text(), nullable=True))
    rows = bind.execute(sa.text("SELECT id, content FROM knowledge_chunks WHERE content_norm IS NULL")).fetchall()
    for i in range(0, len(rows), 500):
        for row in rows[i:i + 500]:
            bind.execute(sa.text("UPDATE knowledge_chunks SET content_norm = :n WHERE id = :id"),
                         {"n": normalize_arabic(row.content or "").casefold(), "id": row.id})
    if pg:
        # 'simple' config: no language stemming, so Arabic and English behave predictably after our own normalization.
        op.execute("CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_fts ON knowledge_chunks "
                   "USING gin (to_tsvector('simple', coalesce(content_norm, '')))")
        op.execute("CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_cosine ON embeddings "
                   "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")

    # --- claims for generated content
    claim_cols = _cols(insp, "extracted_claims")
    if "learning_asset_id" not in claim_cols:
        op.add_column("extracted_claims", sa.Column("learning_asset_id", sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_extracted_claims_learning_asset_id_learning_assets", "extracted_claims",
                              "learning_assets", ["learning_asset_id"], ["id"], ondelete="RESTRICT")
        op.create_index("ix_extracted_claims_learning_asset_id", "extracted_claims", ["learning_asset_id"])
        op.alter_column("extracted_claims", "raw_document_id", existing_type=sa.Uuid(), nullable=True)
        op.create_check_constraint("ck_extracted_claims_has_origin", "extracted_claims",
                                   "raw_document_id IS NOT NULL OR learning_asset_id IS NOT NULL")

    # --- persistent preparation jobs
    if "preparation_jobs" not in insp.get_table_names():
        op.create_table(
            "preparation_jobs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("idempotency_key", sa.String(200), unique=True),
            sa.Column("request", json_type, nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("progress", json_type, nullable=False),
            sa.Column("result", json_type),
            sa.Column("created_by", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_preparation_jobs_status", "preparation_jobs", ["status"])
        op.create_index("ix_preparation_jobs_request_hash", "preparation_jobs", ["request_hash"])
        op.create_index("ix_preparation_jobs_queue", "preparation_jobs", ["status", "priority", "scheduled_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "preparation_jobs" in insp.get_table_names():
        op.drop_table("preparation_jobs")
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_embeddings_hnsw_cosine")
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_fts")
    if "learning_asset_id" in _cols(insp, "extracted_claims"):
        # Claims that originate only in generated assets cannot satisfy NOT NULL again; remove them first.
        op.execute("DELETE FROM evidence_records WHERE claim_id IN "
                   "(SELECT id FROM extracted_claims WHERE raw_document_id IS NULL)")
        op.execute("DELETE FROM verification_jobs WHERE claim_id IN "
                   "(SELECT id FROM extracted_claims WHERE raw_document_id IS NULL)")
        op.execute("DELETE FROM extracted_claims WHERE raw_document_id IS NULL")
        op.drop_constraint("ck_extracted_claims_has_origin", "extracted_claims", type_="check")
        op.drop_index("ix_extracted_claims_learning_asset_id", table_name="extracted_claims")
        op.drop_constraint("fk_extracted_claims_learning_asset_id_learning_assets", "extracted_claims", type_="foreignkey")
        op.drop_column("extracted_claims", "learning_asset_id")
        op.alter_column("extracted_claims", "raw_document_id", existing_type=sa.Uuid(), nullable=False)
    if "content_norm" in _cols(insp, "knowledge_chunks"):
        op.drop_column("knowledge_chunks", "content_norm")
