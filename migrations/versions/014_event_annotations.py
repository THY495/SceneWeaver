"""Source-bound creator notes for individual scene events."""

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"


def upgrade():
    op.create_table(
        "event_annotations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "event_id", "request_id"),
    )
    for key in ("owner_id", "event_id"):
        op.create_index(f"ix_event_annotations_{key}", "event_annotations", [key])


def downgrade():
    raise RuntimeError("保留人工观察记录，回退应用入口即可。")
