"""Caption delivery and independently acknowledged audio attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "006"
down_revision = "005"


def upgrade():
    op.create_table(
        "voice_deliveries",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("text_visible", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "voice_playbacks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("scene_records.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("voice", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("sent_samples", sa.Integer(), nullable=False),
        sa.Column("played_samples", sa.Integer(), nullable=False),
        sa.Column("receipt_seq", sa.Integer(), nullable=False),
        sa.Column("stream_done", sa.Boolean(), nullable=False),
        sa.Column("timings", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "request_id"),
    )
    for column in ("event_id", "session_id"):
        op.create_index(f"ix_voice_playbacks_{column}", "voice_playbacks", [column])


def downgrade():
    op.drop_table("voice_playbacks")
    op.drop_table("voice_deliveries")
