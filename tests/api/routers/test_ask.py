import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from api.db.models import Query as QueryRow
from api.db.models import Repo, User
from api.db.session import async_session_factory
from api.routers import ask as ask_router
from api.services import auth
from api.services.retrieval import RetrievedChunk


async def _fake_get_secret(key: str) -> str:
    return "test-secret"


@pytest.fixture(autouse=True)
def _patch_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "get_secret", _fake_get_secret)


@pytest.fixture
async def owner_and_repo():
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
        await session.commit()
        await session.refresh(user)
        await session.refresh(repo)
        return user, repo


async def _login(client: AsyncClient, user_id: uuid.UUID) -> None:
    session_jwt = await auth.create_session_jwt(user_id)
    client.cookies.set("session", session_jwt)


def _parse_sse(text: str) -> list[dict]:
    events = []
    for raw_event in text.strip().split("\n\n"):
        lines = raw_event.splitlines()
        event_line = next(line for line in lines if line.startswith("event: "))
        data_line = next(line for line in lines if line.startswith("data: "))
        events.append({"event": event_line[len("event: ") :], "data": json.loads(data_line[len("data: ") :])})
    return events


async def test_ask_requires_auth(client: AsyncClient) -> None:
    response = await client.post(f"/api/v1/repos/{uuid.uuid4()}/ask", json={"question": "hi"})

    assert response.status_code == 401


async def test_ask_404_for_other_users_repo(client: AsyncClient, owner_and_repo) -> None:
    _, repo = owner_and_repo
    async with async_session_factory() as session:
        other_user = User(email="other@example.com", github_login="other")
        session.add(other_user)
        await session.commit()
        await session.refresh(other_user)

    await _login(client, other_user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/ask", json={"question": "hi"})

    assert response.status_code == 404


async def test_ask_409_when_repo_not_ready(client: AsyncClient, owner_and_repo) -> None:
    user, repo = owner_and_repo
    async with async_session_factory() as session:
        repo_row = await session.get(Repo, repo.id)
        repo_row.status = "indexing"
        await session.commit()

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/ask", json={"question": "hi"})

    assert response.status_code == 409


async def test_ask_streams_sources_then_tokens_then_done_and_persists_query(
    client: AsyncClient, owner_and_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, repo = owner_and_repo

    chunk = RetrievedChunk(
        id=uuid.uuid4(),
        file_path="a.py",
        start_line=1,
        end_line=2,
        function_name="foo",
        language="python",
        content="def foo(): pass",
        score=0.9,
    )

    async def fake_hybrid_search(db, repo_id, question):
        return [chunk]

    async def fake_rerank(question, chunks, top_k=5):
        return chunks

    async def fake_stream_answer(question, chunks):
        for token in ["Hello", " world"]:
            yield token

    monkeypatch.setattr(ask_router, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(ask_router, "rerank", fake_rerank)
    monkeypatch.setattr(ask_router, "stream_answer", fake_stream_answer)

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/ask", json={"question": "what does foo do?"})

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [e["event"] for e in events] == ["sources", "token", "token", "done"]
    assert events[0]["data"]["sources"] == [
        {"file_path": "a.py", "start_line": 1, "end_line": 2, "function_name": "foo"}
    ]
    assert events[1]["data"]["text"] == "Hello"
    assert events[2]["data"]["text"] == " world"

    query_id = uuid.UUID(events[3]["data"]["query_id"])
    async with async_session_factory() as session:
        query = await session.get(QueryRow, query_id)
        assert query.answer == "Hello world"
        assert query.retrieved_chunk_ids == [{"chunk_id": str(chunk.id), "score": 0.9}]


async def test_ask_returns_502_when_retrieval_fails_before_streaming(
    client: AsyncClient, owner_and_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, repo = owner_and_repo

    async def failing_hybrid_search(db, repo_id, question):
        raise RuntimeError("Voyage is down")

    monkeypatch.setattr(ask_router, "hybrid_search", failing_hybrid_search)

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/ask", json={"question": "what does foo do?"})

    assert response.status_code == 502
    assert response.json()["error"] == "retrieval_failed"


async def test_ask_emits_error_event_when_answer_generation_fails_mid_stream(
    client: AsyncClient, owner_and_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after streaming has already started (sources/tokens sent)
    can't become a normal HTTP error status — it must show up as an SSE
    `error` event instead of just dropping the connection."""
    user, repo = owner_and_repo

    chunk = RetrievedChunk(
        id=uuid.uuid4(),
        file_path="a.py",
        start_line=1,
        end_line=2,
        function_name="foo",
        language="python",
        content="def foo(): pass",
        score=0.9,
    )

    async def fake_hybrid_search(db, repo_id, question):
        return [chunk]

    async def fake_rerank(question, chunks, top_k=5):
        return chunks

    async def failing_stream_answer(question, chunks):
        yield "partial answer "
        raise RuntimeError("Your credit balance is too low")

    monkeypatch.setattr(ask_router, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(ask_router, "rerank", fake_rerank)
    monkeypatch.setattr(ask_router, "stream_answer", failing_stream_answer)

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/ask", json={"question": "what does foo do?"})

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [e["event"] for e in events] == ["sources", "token", "error"]
    assert "message" in events[2]["data"]

    async with async_session_factory() as session:
        result = await session.execute(select(QueryRow).where(QueryRow.repo_id == repo.id))
        assert result.scalars().all() == []  # failed interaction is not persisted as a query
