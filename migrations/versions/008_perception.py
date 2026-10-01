"""Frozen per-observer excerpts and speaker recognition; legacy observations remain full."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "008"
down_revision = "007"


def upgrade():
    op.add_column(
        "role_observations",
        sa.Column("perception", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade():
    op.drop_column("role_observations", "perception")
