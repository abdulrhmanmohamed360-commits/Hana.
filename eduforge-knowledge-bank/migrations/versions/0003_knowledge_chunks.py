"""Persist searchable passages with document/page provenance."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "0003_knowledge_chunks"
down_revision = "0002_ingestion_job_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "knowledge_chunks" in inspect(op.get_bind()).get_table_names():
        return  # Fresh installs include this table in the legacy metadata-driven 0001 baseline.
    op.create_table(
        "knowledge_chunks",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=True),
        sa.Column("topic_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("language", sa.String(16), nullable=False, server_default="und"),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meta", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["raw_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["domain_id"], ["knowledge_domains.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),
    )
    op.create_index("ix_knowledge_chunks_content_hash", "knowledge_chunks", ["content_hash"])
    op.create_index("ix_knowledge_chunks_document_page", "knowledge_chunks", ["document_id", "page_start", "page_end"])
    op.create_index("ix_knowledge_chunks_domain_topic", "knowledge_chunks", ["domain_id", "topic_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_domain_topic", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_page", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_content_hash", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
