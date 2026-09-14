"""pg_search bm25 index for chunks

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13

See .claude/tasks/pg-search-bm25.md. Replaces idx_chunks_content_fts
(to_tsvector('english', ...)): Postgres's english dictionary stems and
never splits identifiers, so `foo_bar`/`camelCase` only ever match as one
whole lexeme — a question naming a substring of an identifier or a path
component ("bar", "compose", "indexer") never found it. pg_search's
`source_code` tokenizer splits on identifier/path boundaries while still
indexing the whole token, so both exact and substring-of-identifier
queries match (verified interactively against pg_search 0.25.9).
file_path/function_name get the same tokenizer so they can be searched
and boosted the same way as content.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CASCADE: pg_search declares `vector` (pgvector) as a required extension
    # in its control file even though this project doesn't use pgvector
    # (vectors live in Qdrant) — harmless, just an unused installed extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search CASCADE")

    op.execute("DROP INDEX IF EXISTS idx_chunks_content_fts")
    op.execute(
        """
        CREATE INDEX idx_chunks_bm25 ON chunks
        USING bm25 (id, content, file_path, function_name)
        WITH (
            key_field = 'id',
            text_fields = '{
                "content": {"tokenizer": {"type": "source_code"}},
                "file_path": {"tokenizer": {"type": "source_code"}},
                "function_name": {"tokenizer": {"type": "source_code"}}
            }'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunks_bm25")
    op.execute("CREATE INDEX idx_chunks_content_fts ON chunks USING GIN (to_tsvector('english', content))")
    op.execute("DROP EXTENSION IF EXISTS pg_search CASCADE")
    op.execute("DROP EXTENSION IF EXISTS vector")
