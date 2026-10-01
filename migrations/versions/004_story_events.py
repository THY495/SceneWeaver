"""Story grouping and explicitly delivered character observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004"
down_revision = "003"


def upgrade():
    op.create_table(
        "stories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("world", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_stories_owner_id", "stories", ["owner_id"])
    op.add_column("sessions", sa.Column("story_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_sessions_story_id", "sessions", "stories", ["story_id"], ["id"])
    op.create_index("ix_sessions_story_id", "sessions", ["story_id"])
    op.execute("""INSERT INTO stories(id, owner_id, title, world, created_at)
        SELECT id, owner_id, title, COALESCE(current_state->'profile'->>'world', ''), created_at
        FROM sessions WHERE current_state ? 'profile'""")
    op.execute("UPDATE sessions SET story_id=id WHERE current_state ? 'profile'")
    op.create_table(
        "story_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("story_id", "request_id"),
    )
    op.create_index("ix_story_events_story_id", "story_events", ["story_id"])


def downgrade():
    raise RuntimeError(
        "Story event rollback requires restoring a reviewed backup; do not discard character histories."
    )
