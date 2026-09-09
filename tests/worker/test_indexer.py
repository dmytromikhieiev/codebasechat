import uuid as uuid_module

import pytest
from sqlalchemy import select

from api.db.models import Chunk as ChunkRow
from api.db.models import IndexingJob, Repo, User
from api.db.session import async_session_factory
from api.services.repo_fetcher import RepoFile
from worker import indexer


@pytest.fixture
async def repo() -> Repo:
    async with async_session_factory() as session:
        user = User(email="owner@example.com", github_login="owner")
        session.add(user)
        await session.flush()
        repo = Repo(owner_id=user.id, installation_id=1, repo_full_name="octocat/hello-world", default_branch="main")
        session.add(repo)
        await session.commit()
        await session.refresh(repo)
        return repo


@pytest.fixture(autouse=True)
def _stub_vector_store(monkeypatch: pytest.MonkeyPatch):
    """These are covered by test_vector_store.py against real Qdrant —
    stubbed here so fake low-dimension embeddings don't hit a dimension
    mismatch against the real collection config."""

    async def fake_ensure_collection(repo_id) -> None:
        pass

    async def fake_recreate_collection(repo_id) -> None:
        pass

    async def fake_upsert_points(repo_id, points) -> None:
        pass

    async def fake_delete_points_by_file_path(repo_id, file_path) -> None:
        pass

    monkeypatch.setattr(indexer, "ensure_collection", fake_ensure_collection)
    monkeypatch.setattr(indexer, "recreate_collection", fake_recreate_collection)
    monkeypatch.setattr(indexer, "upsert_points", fake_upsert_points)
    monkeypatch.setattr(indexer, "delete_points_by_file_path", fake_delete_points_by_file_path)


@pytest.fixture(autouse=True)
def _stub_resolve_ref_to_sha(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve_ref_to_sha(installation_id, repo_full_name, ref) -> str:
        return "resolved-sha-1234"

    monkeypatch.setattr(indexer, "resolve_ref_to_sha", fake_resolve_ref_to_sha)


async def test_run_indexing_job_populates_chunks_and_marks_repo_ready(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch_repo_files(installation_id, repo_full_name, ref) -> list[RepoFile]:
        return [
            RepoFile(path="a.py", content="def foo():\n    return 1\n", language="python"),
            RepoFile(path="b.py", content="def bar():\n    return 2\n", language="python"),
        ]

    async def fake_embed_documents(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(indexer, "fetch_repo_files", fake_fetch_repo_files)
    monkeypatch.setattr(indexer, "embed_documents", fake_embed_documents)

    await indexer._run_indexing_job(repo.id)

    async with async_session_factory() as session:
        refreshed_repo = await session.get(Repo, repo.id)
        assert refreshed_repo.status == "ready"
        assert refreshed_repo.indexed_at is not None
        assert refreshed_repo.last_indexed_sha == "resolved-sha-1234"

        result = await session.execute(select(ChunkRow).where(ChunkRow.repo_id == repo.id))
        chunks = result.scalars().all()
        assert {c.function_name for c in chunks} == {"foo", "bar"}
        assert all(c.content_hash for c in chunks)

        result = await session.execute(select(IndexingJob).where(IndexingJob.repo_id == repo.id))
        job = result.scalars().one()
        assert job.status == "succeeded"
        assert job.progress == 1.0
        assert job.files_done == 2
        assert job.files_total == 2


async def test_full_reindex_recreates_collection_instead_of_just_ensuring_it(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full reindex must drop old Qdrant points, not just add new ones on
    top — upsert-only would leave points from the previous run orphaned
    forever. See vector_store.recreate_collection."""

    async def fake_fetch_repo_files(installation_id, repo_full_name, ref) -> list[RepoFile]:
        return [RepoFile(path="a.py", content="def foo():\n    return 1\n", language="python")]

    async def fake_embed_documents(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    recreate_calls = []
    ensure_calls = []

    async def fake_recreate_collection(repo_id) -> None:
        recreate_calls.append(repo_id)

    async def fake_ensure_collection(repo_id) -> None:
        ensure_calls.append(repo_id)

    monkeypatch.setattr(indexer, "fetch_repo_files", fake_fetch_repo_files)
    monkeypatch.setattr(indexer, "embed_documents", fake_embed_documents)
    monkeypatch.setattr(indexer, "recreate_collection", fake_recreate_collection)
    monkeypatch.setattr(indexer, "ensure_collection", fake_ensure_collection)

    await indexer._run_indexing_job(repo.id)

    assert recreate_calls == [repo.id]
    assert ensure_calls == []


async def test_run_indexing_job_marks_failed_on_error(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_repo_files(installation_id, repo_full_name, ref):
        raise RuntimeError("GitHub is down")

    monkeypatch.setattr(indexer, "fetch_repo_files", fake_fetch_repo_files)

    with pytest.raises(RuntimeError, match="GitHub is down"):
        await indexer._run_indexing_job(repo.id)

    async with async_session_factory() as session:
        refreshed_repo = await session.get(Repo, repo.id)
        assert refreshed_repo.status == "failed"

        result = await session.execute(select(IndexingJob).where(IndexingJob.repo_id == repo.id))
        job = result.scalars().one()
        assert job.status == "failed"
        assert "GitHub is down" in job.error


def test_run_indexing_job_sync_wrapper(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_repo_files(installation_id, repo_full_name, ref) -> list[RepoFile]:
        return []

    monkeypatch.setattr(indexer, "fetch_repo_files", fake_fetch_repo_files)

    indexer.run_indexing_job(str(repo.id))


async def test_incremental_indexing_adds_modifies_and_removes_chunks(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session_factory() as session:
        repo_row = await session.get(Repo, repo.id)
        repo_row.last_indexed_sha = "base-sha"
        session.add(
            ChunkRow(
                repo_id=repo.id,
                file_path="removed.py",
                start_line=1,
                end_line=1,
                function_name="gone",
                language="python",
                content="def gone(): pass",
                content_hash="old-hash",
                embedding_id=uuid_module.uuid4(),
            )
        )
        await session.commit()

    async def fake_compare_commits(installation_id, repo_full_name, base, head) -> list[dict]:
        assert base == "base-sha"
        assert head == "head-sha"
        return [
            {"filename": "added.py", "status": "added"},
            {"filename": "removed.py", "status": "removed"},
        ]

    async def fake_fetch_repo_files(installation_id, repo_full_name, ref) -> list[RepoFile]:
        assert ref == "head-sha"
        return [RepoFile(path="added.py", content="def added():\n    return 1\n", language="python")]

    async def fake_embed_documents(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(indexer, "compare_commits", fake_compare_commits)
    monkeypatch.setattr(indexer, "fetch_repo_files", fake_fetch_repo_files)
    monkeypatch.setattr(indexer, "embed_documents", fake_embed_documents)

    await indexer._run_incremental_indexing_job(repo.id, "base-sha", "head-sha")

    async with async_session_factory() as session:
        refreshed_repo = await session.get(Repo, repo.id)
        assert refreshed_repo.status == "ready"
        assert refreshed_repo.last_indexed_sha == "head-sha"

        result = await session.execute(select(ChunkRow).where(ChunkRow.repo_id == repo.id))
        chunk_paths = {c.file_path for c in result.scalars().all()}
        assert chunk_paths == {"added.py"}


async def test_incremental_indexing_renamed_file_moves_chunks(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session_factory() as session:
        repo_row = await session.get(Repo, repo.id)
        repo_row.last_indexed_sha = "base-sha"
        session.add(
            ChunkRow(
                repo_id=repo.id,
                file_path="old_name.py",
                start_line=1,
                end_line=1,
                function_name="foo",
                language="python",
                content="def foo(): pass",
                content_hash="old-hash",
                embedding_id=uuid_module.uuid4(),
            )
        )
        await session.commit()

    async def fake_compare_commits(installation_id, repo_full_name, base, head) -> list[dict]:
        return [{"filename": "new_name.py", "previous_filename": "old_name.py", "status": "renamed"}]

    async def fake_fetch_repo_files(installation_id, repo_full_name, ref) -> list[RepoFile]:
        return [RepoFile(path="new_name.py", content="def foo():\n    return 1\n", language="python")]

    async def fake_embed_documents(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(indexer, "compare_commits", fake_compare_commits)
    monkeypatch.setattr(indexer, "fetch_repo_files", fake_fetch_repo_files)
    monkeypatch.setattr(indexer, "embed_documents", fake_embed_documents)

    await indexer._run_incremental_indexing_job(repo.id, "base-sha", "head-sha")

    async with async_session_factory() as session:
        result = await session.execute(select(ChunkRow).where(ChunkRow.repo_id == repo.id))
        chunk_paths = {c.file_path for c in result.scalars().all()}
        assert chunk_paths == {"new_name.py"}


async def test_index_file_commits_postgres_before_writing_to_qdrant(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the two writes should leave a Postgres row with a
    missing vector (degraded but self-healing), never a Qdrant point with
    no Postgres row behind it (a silent, permanent orphan)."""

    async def fake_embed_documents(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def fake_upsert_points(repo_id, points) -> None:
        async with async_session_factory() as session:
            result = await session.execute(select(ChunkRow).where(ChunkRow.repo_id == repo_id))
            assert result.scalars().all(), "chunk rows must already be committed by the time Qdrant is written"

    monkeypatch.setattr(indexer, "embed_documents", fake_embed_documents)
    monkeypatch.setattr(indexer, "upsert_points", fake_upsert_points)

    file = RepoFile(path="a.py", content="def foo():\n    return 1\n", language="python")
    await indexer._index_file(repo.id, file)


async def test_remove_file_deletes_postgres_before_deleting_from_qdrant(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session_factory() as session:
        session.add(
            ChunkRow(
                repo_id=repo.id,
                file_path="old.py",
                start_line=1,
                end_line=1,
                function_name=None,
                language="python",
                content="x = 1",
                content_hash="h",
                embedding_id=uuid_module.uuid4(),
            )
        )
        await session.commit()

    async def fake_delete_points_by_file_path(repo_id, file_path) -> None:
        async with async_session_factory() as session:
            result = await session.execute(
                select(ChunkRow).where(ChunkRow.repo_id == repo_id, ChunkRow.file_path == file_path)
            )
            assert result.scalars().all() == [], "Postgres row must already be deleted before Qdrant is touched"

    monkeypatch.setattr(indexer, "delete_points_by_file_path", fake_delete_points_by_file_path)

    await indexer._remove_file(repo.id, "old.py")
