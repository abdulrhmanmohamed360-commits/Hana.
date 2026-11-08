"""Stage 9: curriculum hierarchy, coverage profiles, graph edges, gaps, scans, quality assessments.

Like 0005 this is idempotent because a fresh database already received these objects from the 0001 baseline.
The new tables are created from the model metadata (checkfirst) so definitions never drift from the models;
downgrade drops them in dependency order and removes learning_assets.curriculum_node_id.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

import app.models  # noqa: F401
from app.db.base import Base

revision = "0006_curriculum_graph_quality"
down_revision = "0005_verification_hybrid_prepare"
branch_labels = None
depends_on = None

# creation order respects foreign keys
NEW_TABLES = ["curricula", "curriculum_nodes", "coverage_profiles", "graph_edges", "scan_schedules", "scan_runs",
              "knowledge_gaps", "quality_assessments"]


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("learning_assets")}
    existing = set(insp.get_table_names())
    for name in NEW_TABLES:
        if name not in existing:
            Base.metadata.tables[name].create(bind=bind, checkfirst=True)
    if "curriculum_node_id" not in cols:
        op.add_column("learning_assets", sa.Column("curriculum_node_id", sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_learning_assets_curriculum_node_id_curriculum_nodes", "learning_assets",
                              "curriculum_nodes", ["curriculum_node_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_learning_assets_curriculum_node_id", "learning_assets", ["curriculum_node_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "curriculum_node_id" in {c["name"] for c in insp.get_columns("learning_assets")}:
        op.drop_index("ix_learning_assets_curriculum_node_id", table_name="learning_assets")
        op.drop_constraint("fk_learning_assets_curriculum_node_id_curriculum_nodes", "learning_assets", type_="foreignkey")
        op.drop_column("learning_assets", "curriculum_node_id")
    for name in reversed(NEW_TABLES):
        if name in set(inspect(bind).get_table_names()):
            op.drop_table(name)
