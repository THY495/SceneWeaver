"""Add reconstructible typed indices without copying private event content."""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"


def upgrade():
    op.create_table(
        "event_authority",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("source_class", sa.String(32), nullable=False),
    )
    op.create_index("ix_event_authority_source_class", "event_authority", ["source_class"])
    op.create_table(
        "speech_acts",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("speaker_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=True),
    )
    op.create_index("ix_speech_acts_speaker_id", "speech_acts", ["speaker_id"])
    op.create_table(
        "interaction_requests",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("asker_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
    )
    for name in ("asker_id", "target_id"):
        op.create_index(f"ix_interaction_requests_{name}", "interaction_requests", [name])
    op.create_table(
        "request_transitions",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column(
            "request_id", sa.String(36), sa.ForeignKey("interaction_requests.event_id"), primary_key=True
        ),
        sa.Column("status", sa.String(24), nullable=False),
    )


def downgrade():
    for name in ("request_transitions", "interaction_requests", "speech_acts", "event_authority"):
        op.drop_table(name)
