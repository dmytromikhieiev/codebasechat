import uuid

import pytest
from httpx import AsyncClient

from api.db.models import IndexingJob, Repo, User
from api.db.session import async_session_factory
from api.routers import repos as repos_router
from api.services import auth


async def _login(client: AsyncClient, user_id: uuid.UUID) -> None:
    session_jwt = await auth.create_session_jwt(user_id)
    client.cookies.set("session", session_jwt)


@pytest.fixture
async def owner_and_repo() -> tuple[User, Repo]:
    async with async_session_factory() as session:
        user = User(email="owner@example.com", github_login="owner")
        session.add(user)
        await session.flush()
        repo = Repo(
            owner_id=user.id,
            installation_id=1,
            repo_full_name="octocat/hello-world",
            default_branch="main",
            status="ready",
            last_indexed_sha="base-sha",
        )
        session.add(repo)
        await session.commit()
        await session.refresh(user)
        await session.refresh(repo)
        return user, repo


async def test_reindex_requires_auth(client: AsyncClient) -> None:
    response = await client.post(f"/api/v1/repos/{uuid.uuid4()}/reindex")

    assert response.status_code == 401


async def test_reindex_404_for_other_users_repo(client: AsyncClient, owner_and_repo) -> None:
    _, repo = owner_and_repo
    async with async_session_factory() as session:
        other_user = User(email="other@example.com", github_login="other")
        session.add(other_user)
        await session.commit()
        await session.refresh(other_user)

    await _login(client, other_user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/reindex")

    assert response.status_code == 404


async def test_reindex_409_while_already_indexing(client: AsyncClient, owner_and_repo) -> None:
    user, repo = owner_and_repo
    async with async_session_factory() as session:
        repo_row = await session.get(Repo, repo.id)
        repo_row.status = "indexing"
        await session.commit()

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/reindex")

    assert response.status_code == 409
    assert response.json()["error"] == "already_indexing"


async def test_reindex_queues_full_job_when_never_indexed(
    client: AsyncClient, owner_and_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, repo = owner_and_repo
    async with async_session_factory() as session:
        repo_row = await session.get(Repo, repo.id)
        repo_row.last_indexed_sha = None
        await session.commit()

    calls = []

    async def fake_enqueue_full(repo_id) -> None:
        calls.append(repo_id)

    monkeypatch.setattr(repos_router, "enqueue_indexing_job", fake_enqueue_full)

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/reindex")

    assert response.status_code == 200
    assert response.json()["status"] == "queued_full"
    assert calls == [repo.id]


async def test_reindex_returns_up_to_date_when_head_matches(
    client: AsyncClient, owner_and_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, repo = owner_and_repo

    async def fake_resolve_ref_to_sha(installation_id, repo_full_name, ref) -> str:
        return "base-sha"

    incremental_calls = []

    async def fake_enqueue_incremental(repo_id, base_sha, head_sha) -> None:
        incremental_calls.append((repo_id, base_sha, head_sha))

    monkeypatch.setattr(repos_router, "resolve_ref_to_sha", fake_resolve_ref_to_sha)
    monkeypatch.setattr(repos_router, "enqueue_incremental_indexing_job", fake_enqueue_incremental)

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/reindex")

    assert response.status_code == 200
    assert response.json()["status"] == "up_to_date"
    assert incremental_calls == []


async def test_reindex_queues_incremental_job_when_head_changed(
    client: AsyncClient, owner_and_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, repo = owner_and_repo

    async def fake_resolve_ref_to_sha(installation_id, repo_full_name, ref) -> str:
        return "new-head-sha"

    calls = []

    async def fake_enqueue_incremental(repo_id, base_sha, head_sha) -> None:
        calls.append((repo_id, base_sha, head_sha))

    monkeypatch.setattr(repos_router, "resolve_ref_to_sha", fake_resolve_ref_to_sha)
    monkeypatch.setattr(repos_router, "enqueue_incremental_indexing_job", fake_enqueue_incremental)

    await _login(client, user.id)
    response = await client.post(f"/api/v1/repos/{repo.id}/reindex")

    assert response.status_code == 200
    assert response.json()["status"] == "queued_incremental"
    assert calls == [(repo.id, "base-sha", "new-head-sha")]


async def test_list_repos_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/repos")

    assert response.status_code == 401


async def test_list_repos_returns_only_current_users_repos(client: AsyncClient, owner_and_repo) -> None:
    user, repo = owner_and_repo
    async with async_session_factory() as session:
        other_user = User(email="other@example.com", github_login="other")
        session.add(other_user)
        await session.flush()
        session.add(
            Repo(
                owner_id=other_user.id,
                installation_id=2,
                repo_full_name="other/repo",
                default_branch="main",
                status="ready",
            )
        )
        await session.commit()

    await _login(client, user.id)
    response = await client.get("/api/v1/repos")

    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body] == [str(repo.id)]


async def test_get_repo_404_for_other_users_repo(client: AsyncClient, owner_and_repo) -> None:
    _, repo = owner_and_repo
    async with async_session_factory() as session:
        other_user = User(email="other2@example.com", github_login="other2")
        session.add(other_user)
        await session.commit()
        await session.refresh(other_user)

    await _login(client, other_user.id)
    response = await client.get(f"/api/v1/repos/{repo.id}")

    assert response.status_code == 404


async def test_get_repo_includes_latest_job_progress(client: AsyncClient, owner_and_repo) -> None:
    user, repo = owner_and_repo
    async with async_session_factory() as session:
        session.add(
            IndexingJob(repo_id=repo.id, status="running", progress=0.25, files_done=1, files_total=4)
        )
        await session.commit()

    await _login(client, user.id)
    response = await client.get(f"/api/v1/repos/{repo.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["progress"] == 0.25
    assert body["files_done"] == 1
    assert body["files_total"] == 4


async def test_get_repo_without_any_job_has_null_progress(client: AsyncClient, owner_and_repo) -> None:
    user, repo = owner_and_repo
    await _login(client, user.id)

    response = await client.get(f"/api/v1/repos/{repo.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["progress"] is None
