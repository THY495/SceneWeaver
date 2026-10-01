"""Index for bounded single-machine semantic retrieval."""

from alembic import op

revision = "002"
down_revision = "001"


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
