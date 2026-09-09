import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete

from api.db.models import Chunk as ChunkRow
from api.db.models import IndexingJob, Repo
from api.db.session import async_session_factory
from api.services import chunking
from api.services.embeddings import embed_documents
from api.services.github_app import compare_commits, resolve_ref_to_sha
from api.services.repo_fetcher import RepoFile, fetch_repo_files
from api.services.vector_store import (
    VectorPoint,
    delete_points_by_file_path,
    ensure_collection,
    recreate_collection,
    upsert_points,
)

logger = logging.getLogger(__name__)


def run_indexing_job(repo_id: str) -> None:
    """Sync entrypoint RQ calls — bridges into the async pipeline below."""
    asyncio.run(_run_indexing_job(uuid.UUID(repo_id)))


def run_incremental_indexing_job(repo_id: str, base_sha: str, head_sha: str) -> None:
    """Sync entrypoint RQ calls for a push webhook — see api/routers/webhooks.py."""
    asyncio.run(_run_incremental_indexing_job(uuid.UUID(repo_id), base_sha, head_sha))


async def _run_indexing_job(repo_id: uuid.UUID) -> None:
    job_id = await _start_indexing_job(repo_id)
    if job_id is None:
        return
    try:
        await _index_repo(repo_id, job_id)
    except Exception as exc:
        await _mark_job_failed(repo_id, job_id, exc)
        raise


async def _run_incremental_indexing_job(repo_id: uuid.UUID, base_sha: str, head_sha: str) -> None:
    job_id = await _start_indexing_job(repo_id)
    if job_id is None:
        return
    try:
        await _index_repo_incremental(repo_id, job_id, base_sha, head_sha)
    except Exception as exc:
        await _mark_job_failed(repo_id, job_id, exc)
        raise


async def _start_indexing_job(repo_id: uuid.UUID) -> uuid.UUID | None:
    async with async_session_factory() as db:
        repo = await db.get(Repo, repo_id)
        if repo is None:
            logger.warning("indexing job for missing repo_id=%s, skipping", repo_id)
            return None

        job = IndexingJob(repo_id=repo_id, status="running", started_at=_now())
        db.add(job)
        repo.status = "indexing"
        await db.commit()
        await db.refresh(job)
        return job.id


async def _mark_job_failed(repo_id: uuid.UUID, job_id: uuid.UUID, exc: Exception) -> None:
    logger.exception("indexing failed for repo_id=%s", repo_id)
    async with async_session_factory() as db:
        repo = await db.get(Repo, repo_id)
        job_row = await db.get(IndexingJob, job_id)
        if repo is not None:
            repo.status = "failed"
        if job_row is not None:
            job_row.status = "failed"
            job_row.error = str(exc)
            job_row.finished_at = _now()
        await db.commit()


async def _index_repo(repo_id: uuid.UUID, job_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        repo = await db.get(Repo, repo_id)
        installation_id = repo.installation_id
        repo_full_name = repo.repo_full_name
        default_branch = repo.default_branch

    resolved_sha = await resolve_ref_to_sha(installation_id, repo_full_name, default_branch)
    files = await fetch_repo_files(installation_id, repo_full_name, default_branch)

    async with async_session_factory() as db:
        job = await db.get(IndexingJob, job_id)
        job.files_total = len(files)
        await db.commit()

    # Full reindex — every chunk of the repo is rebuilt from scratch, so the
    # Qdrant collection is dropped and recreated rather than just ensured:
    # upsert alone would leave last run's points orphaned in it forever.
    await recreate_collection(repo_id)

    async with async_session_factory() as db:
        await db.execute(delete(ChunkRow).where(ChunkRow.repo_id == repo_id))
        await db.commit()

    for files_done, file in enumerate(files, start=1):
        await _index_file(repo_id, file)

        async with async_session_factory() as db:
            job = await db.get(IndexingJob, job_id)
            job.files_done = files_done
            job.progress = files_done / len(files) if files else 1.0
            await db.commit()

    async with async_session_factory() as db:
        repo = await db.get(Repo, repo_id)
        repo.status = "ready"
        repo.indexed_at = _now()
        repo.last_indexed_sha = resolved_sha

        job = await db.get(IndexingJob, job_id)
        job.status = "succeeded"
        job.progress = 1.0
        job.finished_at = _now()

        await db.commit()


async def _index_repo_incremental(repo_id: uuid.UUID, job_id: uuid.UUID, base_sha: str, head_sha: str) -> None:
    async with async_session_factory() as db:
        repo = await db.get(Repo, repo_id)
        installation_id = repo.installation_id
        repo_full_name = repo.repo_full_name

    changed_files = await compare_commits(installation_id, repo_full_name, base_sha, head_sha)

    async with async_session_factory() as db:
        job = await db.get(IndexingJob, job_id)
        job.files_total = len(changed_files)
        await db.commit()

    await ensure_collection(repo_id)

    # Fetching the whole tree at head_sha to get content for changed files —
    # simpler than point-fetching via the Contents API, at the cost of an
    # extra full tarball download per push (see webhooks.md limitations).
    files_at_head = {f.path: f for f in await fetch_repo_files(installation_id, repo_full_name, head_sha)}

    for files_done, changed in enumerate(changed_files, start=1):
        status = changed.get("status")
        filename = changed["filename"]

        if status == "renamed":
            await _remove_file(repo_id, changed.get("previous_filename") or filename)
        else:
            await _remove_file(repo_id, filename)

        if status != "removed":
            repo_file = files_at_head.get(filename)
            if repo_file is not None:
                await _index_file(repo_id, repo_file)

        async with async_session_factory() as db:
            job = await db.get(IndexingJob, job_id)
            job.files_done = files_done
            job.progress = files_done / len(changed_files) if changed_files else 1.0
            await db.commit()

    async with async_session_factory() as db:
        repo = await db.get(Repo, repo_id)
        repo.status = "ready"
        repo.indexed_at = _now()
        repo.last_indexed_sha = head_sha

        job = await db.get(IndexingJob, job_id)
        job.status = "succeeded"
        job.progress = 1.0
        job.finished_at = _now()

        await db.commit()


async def _remove_file(repo_id: uuid.UUID, file_path: str) -> None:
    # Postgres first: it's what retrieval actually queries (BM25 + the join
    # in retrieval._load_chunks). If this crashes right after, Qdrant keeps
    # a harmless, invisible orphan point — better than the reverse order,
    # where a crash would leave a Postgres row still findable via BM25 for
    # content we meant to delete.
    async with async_session_factory() as db:
        await db.execute(delete(ChunkRow).where(ChunkRow.repo_id == repo_id, ChunkRow.file_path == file_path))
        await db.commit()
    await delete_points_by_file_path(repo_id, file_path)


async def _index_file(repo_id: uuid.UUID, file: RepoFile) -> None:
    chunks = chunking.chunk_file(file.path, file.content)
    if not chunks:
        return

    vectors = await embed_documents([chunk.content for chunk in chunks])

    points: list[VectorPoint] = []
    chunk_rows: list[ChunkRow] = []
    for chunk, vector in zip(chunks, vectors):
        embedding_id = uuid.uuid4()
        points.append(
            VectorPoint(
                id=embedding_id,
                vector=vector,
                file_path=chunk.file_path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                function_name=chunk.function_name,
            )
        )
        chunk_rows.append(
            ChunkRow(
                repo_id=repo_id,
                file_path=chunk.file_path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                function_name=chunk.function_name,
                language=chunk.language,
                content=chunk.content,
                content_hash=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                embedding_id=embedding_id,
            )
        )

    # Postgres first, Qdrant second — same reasoning as _remove_file: a
    # crash between the two writes should leave a chunk that's missing its
    # vector (findable via BM25, self-heals on the next reindex) rather
    # than a vector with no Postgres row behind it (a silent, permanent
    # orphan — retrieval only ever looks chunks up by querying Postgres).
    async with async_session_factory() as db:
        db.add_all(chunk_rows)
        await db.commit()

    await upsert_points(repo_id, points)


def _now() -> datetime:
    return datetime.now(timezone.utc)
