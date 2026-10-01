"""Reusable role cards with immutable revision records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003"
down_revision = "002"


def upgrade():
    op.create_table(
        "character_cards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_character_cards_owner_id", "character_cards", ["owner_id"])
    op.create_table(
        "character_revisions",
        sa.Column("card_id", sa.String(36), sa.ForeignKey("character_cards.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("character_revisions")
    op.drop_index("ix_character_cards_owner_id", table_name="character_cards")
    op.drop_table("character_cards")
