"""Persist player reading bookmarks independently of browser origins."""

from alembic import op

revision = "017"
down_revision = "016"


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS player_reading (
            session_id VARCHAR(36) REFERENCES sessions(id),
            epoch INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            progress JSONB NOT NULL,
            PRIMARY KEY (session_id, epoch)
        )
    """)


def downgrade():
    raise RuntimeError("保留玩家阅读位置，回退应用入口即可。")
