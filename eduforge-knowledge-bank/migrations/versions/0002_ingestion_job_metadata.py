"""Add metadata fields required by asynchronous document ingestion.

The baseline migration creates tables from current model metadata. Conditional checks
also support databases that already applied the original 0001 baseline.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "0002_ingestion_job_metadata"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("ingestion_jobs")}
    if "created_by" not in columns:
        op.add_column("ingestion_jobs", sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"))
    if "meta" not in columns:
        op.add_column("ingestion_jobs", sa.Column("meta", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False, server_default="{}"))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("ingestion_jobs")}
    if "meta" in columns:
        op.drop_column("ingestion_jobs", "meta")
    if "created_by" in columns:
        op.drop_column("ingestion_jobs", "created_by")
