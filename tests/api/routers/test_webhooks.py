import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient

from api.db.models import Repo, User
from api.db.session import async_session_factory
from api.routers import webhooks as webhooks_router

WEBHOOK_SECRET = "test-webhook-secret"


async def _fake_get_secret(key: str) -> str:
    return WEBHOOK_SECRET


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(webhooks_router, "get_secret", _fake_get_secret)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _post_webhook(client: AsyncClient, event: str, payload: dict, *, valid_signature: bool = True):
    body = json.dumps(payload).encode()
    signature = _sign(body) if valid_signature else "sha256=deadbeef"
    return await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
        },
    )


@pytest.fixture
async def repo() -> Repo:
    async with async_session_factory() as session:
        user = User(email="owner@example.com", github_login="owner")
        session.add(user)
        await session.flush()
        repo = Repo(
            owner_id=user.id,
            installation_id=42,
            repo_full_name="octocat/hello-world",
            default_branch="main",
            status="ready",
            last_indexed_sha="base-sha",
        )
        session.add(repo)
        await session.commit()
        await session.refresh(repo)
        return repo


async def test_invalid_signature_rejected(client: AsyncClient) -> None:
    response = await _post_webhook(client, "push", {"foo": "bar"}, valid_signature=False)

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_webhook_signature"


async def test_missing_signature_rejected(client: AsyncClient) -> None:
    body = json.dumps({"foo": "bar"}).encode()
    response = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push"},
    )

    assert response.status_code == 401


async def test_ping_event_returns_ok_without_action(client: AsyncClient) -> None:
    response = await _post_webhook(client, "ping", {"zen": "hello"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


async def test_push_to_untracked_repo_is_ignored(client: AsyncClient) -> None:
    payload = {
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "repository": {"full_name": "someone/unknown-repo"},
        "installation": {"id": 999},
    }
    response = await _post_webhook(client, "push", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


async def test_push_to_non_default_branch_is_ignored(
    client: AsyncClient, repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued = []

    async def fake_enqueue_incremental(*args) -> None:
        enqueued.append(args)

    async def fake_enqueue_full(*args) -> None:
        enqueued.append(args)

    monkeypatch.setattr(webhooks_router, "enqueue_incremental_indexing_job", fake_enqueue_incremental)
    monkeypatch.setattr(webhooks_router, "enqueue_indexing_job", fake_enqueue_full)

    payload = {
        "ref": "refs/heads/feature-branch",
        "after": "a" * 40,
        "repository": {"full_name": repo.repo_full_name},
        "installation": {"id": repo.installation_id},
    }
    response = await _post_webhook(client, "push", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert enqueued == []


async def test_branch_deletion_push_is_ignored(client: AsyncClient, repo: Repo) -> None:
    payload = {
        "ref": f"refs/heads/{repo.default_branch}",
        "after": "0" * 40,
        "repository": {"full_name": repo.repo_full_name},
        "installation": {"id": repo.installation_id},
    }
    response = await _post_webhook(client, "push", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


async def test_push_to_default_branch_enqueues_incremental_job(
    client: AsyncClient, repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    async def fake_enqueue_incremental(repo_id, base_sha, head_sha) -> None:
        calls.append((repo_id, base_sha, head_sha))

    monkeypatch.setattr(webhooks_router, "enqueue_incremental_indexing_job", fake_enqueue_incremental)

    payload = {
        "ref": f"refs/heads/{repo.default_branch}",
        "after": "new-sha",
        "repository": {"full_name": repo.repo_full_name},
        "installation": {"id": repo.installation_id},
    }
    response = await _post_webhook(client, "push", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert calls == [(repo.id, "base-sha", "new-sha")]


async def test_push_without_prior_index_enqueues_full_job(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session_factory() as session:
        user = User(email="fresh@example.com", github_login="fresh")
        session.add(user)
        await session.flush()
        repo = Repo(
            owner_id=user.id,
            installation_id=7,
            repo_full_name="octocat/fresh-repo",
            default_branch="main",
            status="indexing",
            last_indexed_sha=None,
        )
        session.add(repo)
        await session.commit()
        await session.refresh(repo)

    calls = []

    async def fake_enqueue_full(repo_id) -> None:
        calls.append(repo_id)

    monkeypatch.setattr(webhooks_router, "enqueue_indexing_job", fake_enqueue_full)

    payload = {
        "ref": "refs/heads/main",
        "after": "some-sha",
        "repository": {"full_name": repo.repo_full_name},
        "installation": {"id": repo.installation_id},
    }
    response = await _post_webhook(client, "push", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert calls == [repo.id]
