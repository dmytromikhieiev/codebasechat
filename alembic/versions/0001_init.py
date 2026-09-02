"""init schema

Revision ID: 0001
Revises:
Create Date: 2026-09-02

Baseline schema, carried over verbatim from the old hand-numbered
migrations/0001_init.sql (superseded by this alembic history). Written as
raw SQL rather than op.create_table/op.create_index to guarantee byte-for-
byte the same DDL — including the functional GIN index and the enum types
— as what's already running in every existing environment.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    op.execute(
        """
        CREATE TABLE users (
            id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            email        TEXT NOT NULL UNIQUE,
            github_login TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TYPE repo_status AS ENUM (
            'pending_first_index',
            'indexing',
            'ready',
            'failed'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE repos (
            id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            owner_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            installation_id  BIGINT NOT NULL,
            repo_full_name   TEXT NOT NULL,
            default_branch   TEXT NOT NULL DEFAULT 'main',
            status           repo_status NOT NULL DEFAULT 'pending_first_index',
            last_indexed_sha TEXT,
            indexed_at       TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            UNIQUE (owner_id, repo_full_name)
        )
        """
    )
    op.execute("CREATE INDEX idx_repos_owner_id ON repos (owner_id)")
    op.execute("CREATE INDEX idx_repos_installation_id ON repos (installation_id)")

    op.execute("CREATE TYPE job_status AS ENUM ('queued', 'running', 'succeeded', 'failed')")

    op.execute(
        """
        CREATE TABLE indexing_jobs (
            id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            repo_id      UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            status       job_status NOT NULL DEFAULT 'queued',
            progress     REAL NOT NULL DEFAULT 0,
            files_total  INT,
            files_done   INT,
            error        TEXT,
            started_at   TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_indexing_jobs_repo_id ON indexing_jobs (repo_id, created_at DESC)")

    op.execute(
        """
        CREATE TABLE chunks (
            id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            repo_id        UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            file_path      TEXT NOT NULL,
            start_line     INT NOT NULL,
            end_line       INT NOT NULL,
            function_name  TEXT,
            language       TEXT NOT NULL,
            content        TEXT NOT NULL,
            content_hash   TEXT NOT NULL,
            embedding_id   UUID NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_chunks_repo_id ON chunks (repo_id)")
    op.execute("CREATE INDEX idx_chunks_repo_file ON chunks (repo_id, file_path)")
    op.execute(
        "CREATE INDEX idx_chunks_content_fts ON chunks USING GIN (to_tsvector('english', content))"
    )

    op.execute(
        """
        CREATE TABLE queries (
            id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            repo_id             UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question            TEXT NOT NULL,
            answer              TEXT,
            retrieved_chunk_ids JSONB NOT NULL DEFAULT '[]',
            latency_ms          INT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_queries_repo_id ON queries (repo_id, created_at DESC)")
    op.execute("CREATE INDEX idx_queries_user_id ON queries (user_id, created_at DESC)")

    op.execute(
        """
        CREATE TABLE query_feedback (
            id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            query_id   UUID NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
            rating     SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
            comment    TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_query_feedback_query_id ON query_feedback (query_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS query_feedback")
    op.execute("DROP TABLE IF EXISTS queries")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS indexing_jobs")
    op.execute("DROP TYPE IF EXISTS job_status")
    op.execute("DROP TABLE IF EXISTS repos")
    op.execute("DROP TYPE IF EXISTS repo_status")
    op.execute("DROP TABLE IF EXISTS users")
