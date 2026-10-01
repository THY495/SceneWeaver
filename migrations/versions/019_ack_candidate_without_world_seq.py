"""Unspoken acknowledgement candidates do not consume world event sequence."""

from alembic import op

revision = "019"
down_revision = "018"


def upgrade():
    op.execute("ALTER TABLE scene_records ALTER COLUMN seq DROP NOT NULL")


def downgrade():
    raise RuntimeError("Keep pending acknowledgement candidates until they are released or discarded.")
