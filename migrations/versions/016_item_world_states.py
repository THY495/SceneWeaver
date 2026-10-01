"""Private, per-run authoritative item state."""

from alembic import op

revision = "016"
down_revision = "015"


def upgrade():
    # Also supports development databases initialized with metadata.create_all.
    op.execute("""
        CREATE TABLE IF NOT EXISTS item_world_states (
            story_id VARCHAR(36) PRIMARY KEY REFERENCES stories(id),
            revision INTEGER NOT NULL,
            state JSONB NOT NULL
        )
    """)


def downgrade():
    raise RuntimeError("保留已运行物品状态，回退应用入口即可。")
