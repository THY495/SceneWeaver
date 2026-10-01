"""Per-utterance events, observation scopes and individual character memories."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "005"
down_revision = "004"


def upgrade():
    op.add_column("stories", sa.Column("seq", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("stories", sa.Column("settings", JSONB(), nullable=False, server_default="{}"))
    # The new tables are self-contained. Their schema is frozen in this revision.
    op.create_table(
        "scene_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("sessions.id")),
        sa.Column("actor_name", sa.String(100), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("scene", sa.String(100), nullable=False),
        sa.Column("addressees", JSONB(), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("task_id", sa.String(36)),
        sa.Column("dedupe", sa.String(160), nullable=False),
        sa.Column("occurred_at", sa.String(100)),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("story_id", "seq"),
        sa.UniqueConstraint("story_id", "dedupe"),
    )
    op.create_index("ix_scene_records_story_id", "scene_records", ["story_id"])
    op.create_index("ix_scene_records_task_id", "scene_records", ["task_id"])
    op.create_table(
        "role_observations",
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "role_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_ids", JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("replaces_id", sa.String(36)),
        sa.Column("embedding", Vector(1024)),
        sa.Column("embedding_model", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_role_memories_story_id", "role_memories", ["story_id"])
    op.create_index("ix_role_memories_actor_id", "role_memories", ["actor_id"])
    op.create_table(
        "role_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("covered_seq", sa.Integer(), nullable=False),
        sa.Column("privacy_version", sa.Integer(), nullable=False),
        sa.Column("summary", JSONB(), nullable=False),
        sa.Column("source_ids", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_role_snapshots_actor_id", "role_snapshots", ["actor_id"])
    # Preserve ambiguous legacy text, without inventing speakers, audiences or fictional times.
    op.execute("""INSERT INTO scene_records(id,story_id,seq,kind,actor_name,text,channel,scene,
        addressees,task_id,dedupe,payload,redacted,created_at)
        SELECT m.id,c.story_id,row_number() OVER(PARTITION BY c.story_id ORDER BY m.created_at,m.id),
        'legacy','',m.content,'legacy','unknown','[]'::jsonb,m.task_id,'legacy:'||m.id,
        jsonb_build_object('message_id',m.id,'audience','unknown'),m.redacted,m.created_at
        FROM messages m JOIN sessions c ON c.id=m.session_id
        WHERE c.story_id IS NOT NULL AND c.current_state->>'player'='true'""")
    op.execute(
        "UPDATE stories SET seq=COALESCE((SELECT max(seq) FROM scene_records WHERE story_id=stories.id),0)"
    )
    # Host deliveries already carry explicit recipient IDs and knowledge basis: these are recoverable.
    op.execute("""INSERT INTO scene_records(id,story_id,seq,kind,actor_name,text,channel,scene,
        addressees,dedupe,occurred_at,payload,redacted,created_at)
        SELECT md5(e.id||':'||c.id),e.story_id,
        st.seq+row_number() OVER(PARTITION BY e.story_id ORDER BY e.created_at,e.id,c.id),
        'host','主持人',e.payload->'request'->>'value','private','host',jsonb_build_array(c.id),
        'host:'||e.id||':'||c.id,e.payload->'request'->>'occurred_at',
        jsonb_build_object('key',e.payload->'request'->>'key','value',e.payload->'request'->>'value',
        'event_id',e.id,'occurred_at',e.payload->'request'->>'occurred_at',
        'learned_at',r->>'learned_at','basis',r->>'basis','source',r->>'source'),false,e.created_at
        FROM story_events e CROSS JOIN LATERAL jsonb_array_elements(e.payload->'request'->'recipients') r
        JOIN sessions c ON c.id=r->>'session_id' AND c.story_id=e.story_id
        JOIN stories st ON st.id=e.story_id""")
    op.execute("""INSERT INTO role_observations(actor_id,event_id,created_at)
        SELECT addressees->>0,id,created_at FROM scene_records WHERE kind='host'""")
    op.execute("""INSERT INTO role_memories(id,story_id,actor_id,kind,content,source_ids,status,version,created_at)
        SELECT md5(id||'-episode'),story_id,addressees->>0,'episode',text,jsonb_build_array(id),
        'active',1,created_at FROM scene_records WHERE kind='host'""")
    op.execute(
        "UPDATE stories SET seq=COALESCE((SELECT max(seq) FROM scene_records WHERE story_id=stories.id),0)"
    )


def downgrade():
    raise RuntimeError("Restore a reviewed backup; do not discard character observations and memories.")
