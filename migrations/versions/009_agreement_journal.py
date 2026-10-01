"""Index committed agreement events and persist observation-scoped notification intents."""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"


def upgrade():
    op.create_table(
        "agreement_events",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
    )
    op.create_index("ix_agreement_events_proposal_id", "agreement_events", ["proposal_id"])
    op.create_table(
        "agreement_notices",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("notification_id", sa.Integer, nullable=True),
    )
    op.create_index("ix_agreement_notices_status", "agreement_notices", ["status"])
    op.execute("""
        INSERT INTO agreement_events(event_id, proposal_id, version, kind)
        SELECT id, payload->'agreement'->>'proposal_id',
               (payload->'agreement'->>'version')::integer, payload->'agreement'->>'type'
        FROM scene_records WHERE NOT redacted
          AND payload->'agreement'->>'type' IN ('proposal', 'decision', 'completion')
    """)
    # Historical events are indexed without notifying the player again.


def downgrade():
    raise RuntimeError("协议索引保存来源失效边界，不能删表回退；请保留表并回退应用触发器。")
