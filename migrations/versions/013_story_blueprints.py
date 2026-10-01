"""Editable story blueprints, separate from immutable experiment initials."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "013"
down_revision = "012"


def upgrade():
    op.create_table(
        "story_blueprints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("recipe", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_story_blueprints_owner_id", "story_blueprints", ["owner_id"])


def downgrade():
    raise RuntimeError("保留故事设定数据，回退应用入口即可。")
