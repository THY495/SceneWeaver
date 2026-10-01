"""Independent game save catalogue, separate from creator experiments."""

from alembic import op

revision = "018"
down_revision = "017"


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS game_saves (
            player_id VARCHAR(36) PRIMARY KEY REFERENCES sessions(id),
            story_id VARCHAR(36) NOT NULL UNIQUE REFERENCES stories(id),
            owner_id VARCHAR(80) NOT NULL,
            release_id VARCHAR(64) NOT NULL,
            request_id VARCHAR(80) NOT NULL,
            label VARCHAR(80) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (owner_id, release_id, request_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_game_saves_owner_id ON game_saves(owner_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_game_saves_release_id ON game_saves(release_id)")


def downgrade():
    raise RuntimeError("Preserve player saves; roll back the application instead.")
