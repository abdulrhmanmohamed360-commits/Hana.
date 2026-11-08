"""Add curriculum-scoped educational assets owned by the knowledge bank."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import inspect

revision = "0004_learning_assets"
down_revision = "0003_knowledge_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "learning_assets" in inspect(op.get_bind()).get_table_names():
        return
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "learning_assets",
        sa.Column("education_system", sa.String(100), nullable=False, server_default="general"),
        sa.Column("stage", sa.String(100), nullable=False, server_default="unspecified"),
        sa.Column("grade", sa.String(100), nullable=False, server_default="unspecified"),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("unit", sa.String(200)), sa.Column("lesson", sa.String(240)),
        sa.Column("asset_type", sa.String(40), nullable=False), sa.Column("title", sa.String(300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("language", sa.String(16), nullable=False, server_default="ar"),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sources", json_type, nullable=False, server_default="[]"),
        sa.Column("learning_objectives", json_type, nullable=False, server_default="[]"),
        sa.Column("tags", json_type, nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(128), nullable=False), sa.Column("reviewed_by", sa.String(128)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("meta", json_type, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Uuid(), nullable=False), sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_learning_assets_curriculum", "learning_assets",
                    ["education_system", "stage", "grade", "subject", "unit", "lesson"])
    op.create_index("ix_learning_assets_status_type", "learning_assets", ["status", "asset_type"])


def downgrade() -> None:
    op.drop_index("ix_learning_assets_status_type", table_name="learning_assets")
    op.drop_index("ix_learning_assets_curriculum", table_name="learning_assets")
    op.drop_table("learning_assets")
