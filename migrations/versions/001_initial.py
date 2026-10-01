"""Initial application schema; graph checkpoint schema is managed separately."""

from pathlib import Path

from alembic import op

revision = "001"
down_revision = None


def upgrade():
    for statement in Path(__file__).with_name("001_schema.sql").read_text("utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError(
        "Initial schema rollback requires an explicit backup restore, not destructive DROP ALL."
    )
