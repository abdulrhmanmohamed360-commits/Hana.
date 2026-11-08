"""Document-intelligence upgrade: chunk provenance columns, document file hash, ingestion job stage/lease.

Additive and idempotent. Fresh installs built from model metadata (see 0001) already contain these columns, so every
step is guarded by an inspection of the live schema. No existing column or table is altered or removed.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0007_document_intelligence"
down_revision = "0006_curriculum_graph_quality"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    chunk = _cols("knowledge_chunks")
    if "source_id" not in chunk:
        op.add_column("knowledge_chunks", sa.Column("source_id", sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_knowledge_chunks_source_id_sources", "knowledge_chunks", "sources",
                              ["source_id"], ["id"], ondelete="SET NULL")
    for name, type_ in (("section_title", sa.String(500)), ("chapter_title", sa.String(500)),
                        ("extraction_method", sa.String(32)), ("extraction_confidence", sa.Float())):
        if name not in chunk:
            op.add_column("knowledge_chunks", sa.Column(name, type_, nullable=True))

    if "file_sha256" not in _cols("raw_documents"):
        op.add_column("raw_documents", sa.Column("file_sha256", sa.String(64), nullable=True))
    if "ix_raw_documents_file_sha256" not in _indexes("raw_documents"):
        op.create_index("ix_raw_documents_file_sha256", "raw_documents", ["file_sha256"])

    job = _cols("ingestion_jobs")
    if "stage" not in job:
        op.add_column("ingestion_jobs", sa.Column("stage", sa.String(32), nullable=True))
    if "lease_expires_at" not in job:
        op.add_column("ingestion_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    job = _cols("ingestion_jobs")
    for name in ("lease_expires_at", "stage"):
        if name in job:
            op.drop_column("ingestion_jobs", name)
    if "ix_raw_documents_file_sha256" in _indexes("raw_documents"):
        op.drop_index("ix_raw_documents_file_sha256", table_name="raw_documents")
    if "file_sha256" in _cols("raw_documents"):
        op.drop_column("raw_documents", "file_sha256")
    chunk = _cols("knowledge_chunks")
    if "source_id" in chunk:
        op.drop_constraint("fk_knowledge_chunks_source_id_sources", "knowledge_chunks", type_="foreignkey")
    for name in ("extraction_confidence", "extraction_method", "chapter_title", "section_title", "source_id"):
        if name in chunk:
            op.drop_column("knowledge_chunks", name)
