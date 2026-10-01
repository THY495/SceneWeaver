"""Persistent creator observations, including decisions without scene events."""

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"


def upgrade():
    op.create_table(
        "run_annotations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("story_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("story_seq", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "story_id", "request_id"),
    )
    for key in ("owner_id", "story_id"):
        op.create_index(f"ix_run_annotations_{key}", "run_annotations", [key])


def downgrade():
    raise RuntimeError("保留人工观察笔记，回退应用入口即可。")
