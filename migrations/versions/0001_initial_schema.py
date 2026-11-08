"""Initial schema (Milestone 1 baseline): all core tables known to this baseline + pgvector extension.

NOTE: this baseline builds the tables from the model metadata as of Milestone 1. Every later migration MUST be
explicit (`alembic revision --autogenerate -m "..."`, reviewed by hand). To convert this baseline into an explicit
script before the first production deploy, see docs/ARCHITECTURE.md ("Baseline migration").
"""
from alembic import op

from app.db.base import Base
import app.models  # noqa: F401

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
