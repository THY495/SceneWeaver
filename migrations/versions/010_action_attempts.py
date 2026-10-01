"""Persist execution attempt ownership separately from narrative outcomes."""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"


def upgrade():
    op.create_table(
        "action_attempts",
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("story_id", sa.String(36), sa.ForeignKey("stories.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("task_lease_version", sa.Integer, nullable=False),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("proposal_version", sa.Integer, nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("pause_reason", sa.String(32), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_event_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("story_id", "actor_id", "task_id", "proposal_id", "item_id", "status"):
        op.create_index(f"ix_action_attempts_{column}", "action_attempts", [column])


def downgrade():
    raise RuntimeError("执行尝试保存防重与租约历史，请保留表并回退应用触发器。")
