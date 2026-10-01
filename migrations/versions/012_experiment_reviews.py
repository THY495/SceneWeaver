"""Persist human comparison annotations with source-read identity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "012"
down_revision = "011"


def upgrade():
    op.create_table(
        "experiment_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("left_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("right_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "request_id"),
    )
    for key in ("owner_id", "left_id", "right_id"):
        op.create_index(f"ix_experiment_reviews_{key}", "experiment_reviews", [key])


def downgrade():
    raise RuntimeError("人工标注保存实验依据，请保留表并回退应用入口。")
