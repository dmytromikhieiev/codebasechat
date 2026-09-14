import uuid
from datetime import datetime

from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base

# create_type=False: the type already exists, created by alembic/versions/0001_init.py
repo_status_enum = ENUM(
    "pending_first_index", "indexing", "ready", "failed",
    name="repo_status",
    create_type=False,
)
job_status_enum = ENUM(
    "queued", "running", "succeeded", "failed",
    name="job_status",
    create_type=False,
)


class User(Base):
    """Mirrors the `users` table from alembic/versions/0001_init.py."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    github_login: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class Repo(Base):
    """Mirrors the `repos` table from alembic/versions/0001_init.py."""

    __tablename__ = "repos"
    __table_args__ = (
        UniqueConstraint("owner_id", "repo_full_name"),
        Index("idx_repos_owner_id", "owner_id"),
        Index("idx_repos_installation_id", "installation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default="main")
    status: Mapped[str] = mapped_column(
        repo_status_enum, nullable=False, server_default="pending_first_index"
    )
    last_indexed_sha: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class IndexingJob(Base):
    """Mirrors the `indexing_jobs` table from alembic/versions/0001_init.py."""

    __tablename__ = "indexing_jobs"
    __table_args__ = (Index("idx_indexing_jobs_repo_id", "repo_id", text("created_at DESC")),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(job_status_enum, nullable=False, server_default="queued")
    progress: Mapped[float] = mapped_column(REAL, nullable=False, server_default="0")
    files_total: Mapped[int | None] = mapped_column(Integer)
    files_done: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class Chunk(Base):
    """Mirrors the `chunks` table from alembic/versions/0001_init.py."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("idx_chunks_repo_id", "repo_id"),
        Index("idx_chunks_repo_file", "repo_id", "file_path"),
        # Mirrors alembic/versions/0002_pg_search_bm25.py — idx_chunks_content_fts
        # (to_tsvector('english', ...)) was dropped there in favor of this.
        Index(
            "idx_chunks_bm25",
            "id",
            "content",
            "file_path",
            "function_name",
            postgresql_using="bm25",
            postgresql_with={
                "key_field": "'id'",
                "text_fields": (
                    "'{"
                    '"content": {"tokenizer": {"type": "source_code"}}, '
                    '"file_path": {"tokenizer": {"type": "source_code"}}, '
                    '"function_name": {"tokenizer": {"type": "source_code"}}'
                    "}'"
                ),
            },
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    function_name: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class Query(Base):
    """Mirrors the `queries` table from alembic/versions/0001_init.py."""

    __tablename__ = "queries"
    __table_args__ = (
        Index("idx_queries_repo_id", "repo_id", text("created_at DESC")),
        Index("idx_queries_user_id", "user_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    retrieved_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class QueryFeedback(Base):
    """Mirrors the `query_feedback` table from alembic/versions/0001_init.py."""

    __tablename__ = "query_feedback"
    __table_args__ = (
        CheckConstraint("rating IN (-1, 1)", name="query_feedback_rating_check"),
        Index("idx_query_feedback_query_id", "query_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("queries.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
