import uuid

import pytest

from api.db.models import Chunk as ChunkRow
from api.db.models import Repo, User
from api.db.session import async_session_factory
from api.services import retrieval, vector_store


def _axis_vector(axis: int) -> list[float]:
    vector = [0.0] * vector_store.QDRANT_VECTOR_SIZE
    vector[axis] = 1.0
    return vector


@pytest.fixture
async def repo_with_chunks():
    async with async_session_factory() as session:
        user = User(email="owner@example.com", github_login="owner")
        session.add(user)
        await session.flush()
        repo = Repo(
            owner_id=user.id,
            installation_id=1,
            repo_full_name="octocat/hello",
            default_branch="main",
            status="ready",
        )
        session.add(repo)
        await session.flush()

        bm25_only_id = uuid.uuid4()
        vector_only_id = uuid.uuid4()
        both_id = uuid.uuid4()

        session.add_all(
            [
                ChunkRow(
                    repo_id=repo.id,
                    file_path="bm25_only.py",
                    start_line=1,
                    end_line=1,
                    function_name="bm25_match",
                    language="python",
                    content="the zebra jumps over the fence",
                    content_hash="h1",
                    embedding_id=bm25_only_id,
                ),
                ChunkRow(
                    repo_id=repo.id,
                    file_path="vector_only.py",
                    start_line=1,
                    end_line=1,
                    function_name="vector_match",
                    language="python",
                    content="completely unrelated content about oceans",
                    content_hash="h2",
                    embedding_id=vector_only_id,
                ),
                ChunkRow(
                    repo_id=repo.id,
                    file_path="both.py",
                    start_line=1,
                    end_line=1,
                    function_name="both_match",
                    language="python",
                    content="another zebra reference here",
                    content_hash="h3",
                    embedding_id=both_id,
                ),
            ]
        )
        await session.commit()
        await session.refresh(repo)

    await vector_store.ensure_collection(repo.id)
    await vector_store.upsert_points(
        repo.id,
        [
            # axis 1 keeps bm25_only_id far from the query vector (axis 0) below
            vector_store.VectorPoint(
                id=bm25_only_id, vector=_axis_vector(1), file_path="bm25_only.py", start_line=1, end_line=1, function_name="bm25_match"
            ),
            vector_store.VectorPoint(
                id=vector_only_id, vector=_axis_vector(0), file_path="vector_only.py", start_line=1, end_line=1, function_name="vector_match"
            ),
            vector_store.VectorPoint(
                id=both_id, vector=_axis_vector(0), file_path="both.py", start_line=1, end_line=1, function_name="both_match"
            ),
        ],
    )

    yield repo

    client = vector_store._client()
    await client.delete_collection(vector_store._collection_name(repo.id))


@pytest.fixture(autouse=True)
def _patch_embed_query(monkeypatch: pytest.MonkeyPatch):
    async def fake_embed_query(text: str) -> list[float]:
        return _axis_vector(0)

    monkeypatch.setattr(retrieval, "embed_query", fake_embed_query)


async def test_hybrid_search_combines_bm25_and_vector_only_matches(repo_with_chunks: Repo) -> None:
    async with async_session_factory() as db:
        results = await retrieval.hybrid_search(db, repo_with_chunks.id, "zebra")

    paths = {r.file_path for r in results}
    assert paths == {"bm25_only.py", "vector_only.py", "both.py"}


async def test_hybrid_search_scopes_to_repo(repo_with_chunks: Repo) -> None:
    async with async_session_factory() as db:
        results = await retrieval.hybrid_search(db, uuid.uuid4(), "zebra")

    assert results == []


# --- find_mentioned_file_chunks ---------------------------------------------


async def test_find_mentioned_file_chunks_returns_named_file(repo_with_chunks: Repo) -> None:
    async with async_session_factory() as db:
        results = await retrieval.find_mentioned_file_chunks(db, repo_with_chunks.id, "explain both.py in detail")

    assert [r.file_path for r in results] == ["both.py"]


async def test_find_mentioned_file_chunks_returns_empty_when_no_file_named(repo_with_chunks: Repo) -> None:
    async with async_session_factory() as db:
        results = await retrieval.find_mentioned_file_chunks(db, repo_with_chunks.id, "how does auth work?")

    assert results == []


async def test_find_mentioned_file_chunks_scoped_to_repo(repo_with_chunks: Repo) -> None:
    async with async_session_factory() as db:
        results = await retrieval.find_mentioned_file_chunks(db, uuid.uuid4(), "explain both.py")

    assert results == []


# --- _find_mentioned_file_path (pure, no DB) --------------------------------


def test_find_mentioned_file_path_matches_basename() -> None:
    paths = ["src/api/main.py", "README.md"]

    assert retrieval._find_mentioned_file_path("tell me about README.md", paths) == "README.md"


def test_find_mentioned_file_path_no_match_returns_none() -> None:
    paths = ["src/api/main.py", "README.md"]

    assert retrieval._find_mentioned_file_path("how does retrieval work?", paths) is None


def test_find_mentioned_file_path_prefers_full_path_on_basename_collision() -> None:
    paths = ["services/index.js", "web/index.js"]

    result = retrieval._find_mentioned_file_path("what does web/index.js do?", paths)

    assert result == "web/index.js"


def test_find_mentioned_file_path_falls_back_to_shallowest_on_ambiguous_collision() -> None:
    paths = ["a/deep/nested/index.js", "b/index.js"]

    result = retrieval._find_mentioned_file_path("what does index.js do?", paths)

    assert result == "b/index.js"
