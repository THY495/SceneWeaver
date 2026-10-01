"""Persist immutable experiment starting recipes."""

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiment_initials",
        sa.Column("story_id", sa.String(36), sa.ForeignKey("stories.id"), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "recipe", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "receipt", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "request_id"),
    )
    op.create_index("ix_experiment_initials_owner_id", "experiment_initials", ["owner_id"])


def downgrade():
    raise RuntimeError("实验初始记录保存复现依据，请保留表并回退应用入口。")
