import pytest
from httpx import AsyncClient
from sqlalchemy import select

from api.db.models import Repo, User
from api.db.session import async_session_factory
from api.routers import github as github_router
from api.services import auth


async def _fake_get_secret(key: str) -> str:
    return "test-secret"


@pytest.fixture(autouse=True)
def _patch_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "my-test-app")
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    monkeypatch.setattr(auth, "get_secret", _fake_get_secret)


@pytest.fixture
async def owner_user() -> User:
    async with async_session_factory() as session:
        user = User(email="owner@example.com", github_login="owner")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def test_install_requires_session(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/install", follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["error"] == "not_authenticated"


async def test_install_rejects_invalid_session_cookie(client: AsyncClient) -> None:
    client.cookies.set("session", "not-a-valid-jwt")
    response = await client.get("/api/v1/github/install", follow_redirects=False)

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_session"


async def test_install_redirects_with_signed_state_for_current_user(client: AsyncClient) -> None:
    async with async_session_factory() as session:
        user = User(email="dev@example.com", github_login="dev")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    session_jwt = await auth.create_session_jwt(user.id)
    client.cookies.set("session", session_jwt)

    response = await client.get("/api/v1/github/install", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://github.com/apps/my-test-app/installations/new?state=")

    state = location.split("state=", 1)[1]
    assert await auth.verify_install_state_jwt(state) == user.id


async def test_callback_requires_state(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/callback", params={"installation_id": 1})

    assert response.status_code == 403
    assert response.json()["error"] == "missing_install_state"


async def test_callback_rejects_invalid_state(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/github/callback",
        params={"installation_id": 1, "state": "not-a-jwt"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "invalid_install_state"


async def test_callback_handles_setup_action_request(client: AsyncClient, owner_user: User) -> None:
    state = await auth.create_install_state_jwt(owner_user.id)

    response = await client.get(
        "/api/v1/github/callback",
        params={"setup_action": "request", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:3000?install_status=pending_approval"


async def test_callback_creates_repo_and_enqueues_job(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued = []

    async def fake_enqueue(repo_id) -> None:
        enqueued.append(repo_id)

    async def fake_list_repos(installation_id: int) -> list[dict]:
        return [{"full_name": "octocat/hello-world", "default_branch": "main"}]

    monkeypatch.setattr(github_router, "enqueue_indexing_job", fake_enqueue)
    monkeypatch.setattr(github_router, "list_installation_repositories", fake_list_repos)

    state = await auth.create_install_state_jwt(owner_user.id)
    response = await client.get(
        "/api/v1/github/callback",
        params={"installation_id": 42, "setup_action": "install", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:3000"
    assert len(enqueued) == 1

    async with async_session_factory() as session:
        result = await session.execute(select(Repo).where(Repo.repo_full_name == "octocat/hello-world"))
        repo = result.scalar_one()
        assert repo.owner_id == owner_user.id
        assert repo.installation_id == 42
        assert repo.default_branch == "main"


async def test_callback_upserts_existing_repo_without_duplicating(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_enqueue(repo_id) -> None:
        pass

    async def fake_list_repos(installation_id: int) -> list[dict]:
        return [{"full_name": "octocat/hello-world", "default_branch": "main"}]

    monkeypatch.setattr(github_router, "enqueue_indexing_job", fake_enqueue)
    monkeypatch.setattr(github_router, "list_installation_repositories", fake_list_repos)

    state = await auth.create_install_state_jwt(owner_user.id)
    for _ in range(2):
        response = await client.get(
            "/api/v1/github/callback",
            params={"installation_id": 42, "setup_action": "install", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 302

    async with async_session_factory() as session:
        result = await session.execute(select(Repo).where(Repo.repo_full_name == "octocat/hello-world"))
        assert len(result.scalars().all()) == 1


async def test_sync_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/github/sync")

    assert response.status_code == 401


async def test_sync_404_when_no_matching_installation(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list_installations() -> list[dict]:
        return [{"id": 1, "account": {"login": "someone-else"}}]

    monkeypatch.setattr(github_router, "list_app_installations", fake_list_installations)

    session_jwt = await auth.create_session_jwt(owner_user.id)
    client.cookies.set("session", session_jwt)
    response = await client.post("/api/v1/github/sync")

    assert response.status_code == 404
    assert response.json()["error"] == "installation_not_found"


async def test_sync_matches_installation_by_login_case_insensitively(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued = []

    async def fake_enqueue(repo_id) -> None:
        enqueued.append(repo_id)

    async def fake_list_installations() -> list[dict]:
        return [
            {"id": 99, "account": {"login": "OWNER"}},
            {"id": 100, "account": {"login": "someone-else"}},
        ]

    async def fake_list_repos(installation_id: int) -> list[dict]:
        assert installation_id == 99
        return [{"full_name": "octocat/hello-world", "default_branch": "main"}]

    monkeypatch.setattr(github_router, "list_app_installations", fake_list_installations)
    monkeypatch.setattr(github_router, "list_installation_repositories", fake_list_repos)
    monkeypatch.setattr(github_router, "enqueue_indexing_job", fake_enqueue)

    session_jwt = await auth.create_session_jwt(owner_user.id)
    client.cookies.set("session", session_jwt)
    response = await client.post("/api/v1/github/sync")

    assert response.status_code == 200
    assert response.json()["synced_repos"] == ["octocat/hello-world"]
    assert len(enqueued) == 1

    async with async_session_factory() as session:
        result = await session.execute(select(Repo).where(Repo.repo_full_name == "octocat/hello-world"))
        repo = result.scalar_one()
        assert repo.owner_id == owner_user.id
        assert repo.installation_id == 99


async def test_sync_with_repo_full_name_syncs_only_that_repo(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued = []

    async def fake_enqueue(repo_id) -> None:
        enqueued.append(repo_id)

    async def fake_list_installations() -> list[dict]:
        return [{"id": 99, "account": {"login": "owner"}}]

    async def fake_list_repos(installation_id: int) -> list[dict]:
        return [
            {"full_name": "octocat/hello-world", "default_branch": "main"},
            {"full_name": "octocat/other-repo", "default_branch": "main"},
        ]

    monkeypatch.setattr(github_router, "list_app_installations", fake_list_installations)
    monkeypatch.setattr(github_router, "list_installation_repositories", fake_list_repos)
    monkeypatch.setattr(github_router, "enqueue_indexing_job", fake_enqueue)

    session_jwt = await auth.create_session_jwt(owner_user.id)
    client.cookies.set("session", session_jwt)
    response = await client.post("/api/v1/github/sync", json={"repo_full_name": "octocat/other-repo"})

    assert response.status_code == 200
    assert response.json()["synced_repos"] == ["octocat/other-repo"]
    assert len(enqueued) == 1

    async with async_session_factory() as session:
        result = await session.execute(select(Repo))
        repos = result.scalars().all()
        assert [r.repo_full_name for r in repos] == ["octocat/other-repo"]


async def test_sync_with_unknown_repo_full_name_returns_404(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list_installations() -> list[dict]:
        return [{"id": 99, "account": {"login": "owner"}}]

    async def fake_list_repos(installation_id: int) -> list[dict]:
        return [{"full_name": "octocat/hello-world", "default_branch": "main"}]

    monkeypatch.setattr(github_router, "list_app_installations", fake_list_installations)
    monkeypatch.setattr(github_router, "list_installation_repositories", fake_list_repos)

    session_jwt = await auth.create_session_jwt(owner_user.id)
    client.cookies.set("session", session_jwt)
    response = await client.post("/api/v1/github/sync", json={"repo_full_name": "octocat/does-not-exist"})

    assert response.status_code == 404
    assert response.json()["error"] == "repo_not_found_in_installation"


async def test_available_repos_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/github/available-repos")

    assert response.status_code == 401


async def test_available_repos_404_when_no_matching_installation(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list_installations() -> list[dict]:
        return []

    monkeypatch.setattr(github_router, "list_app_installations", fake_list_installations)

    session_jwt = await auth.create_session_jwt(owner_user.id)
    client.cookies.set("session", session_jwt)
    response = await client.get("/api/v1/github/available-repos")

    assert response.status_code == 404
    assert response.json()["error"] == "installation_not_found"


async def test_available_repos_lists_repos_from_matching_installation(
    client: AsyncClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list_installations() -> list[dict]:
        return [{"id": 99, "account": {"login": "owner"}}]

    async def fake_list_repos(installation_id: int) -> list[dict]:
        return [{"full_name": "octocat/hello-world", "default_branch": "main"}]

    monkeypatch.setattr(github_router, "list_app_installations", fake_list_installations)
    monkeypatch.setattr(github_router, "list_installation_repositories", fake_list_repos)

    session_jwt = await auth.create_session_jwt(owner_user.id)
    client.cookies.set("session", session_jwt)
    response = await client.get("/api/v1/github/available-repos")

    assert response.status_code == 200
    assert response.json() == [
        {"installation_id": 99, "repo_full_name": "octocat/hello-world", "default_branch": "main"}
    ]
