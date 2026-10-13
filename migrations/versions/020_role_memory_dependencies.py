"""Separate quoted memory anchors and model derivation dependencies."""

from alembic import op

from chatbot.memory_dependencies import migrate_dependencies

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade():
    migrate_dependencies(op.get_bind())


def downgrade():
    op.drop_column("role_memories", "context_source_ids")
