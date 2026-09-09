from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import AsyncClient, Response
from sqlalchemy import select

from api.db.models import User
from api.db.session import async_session_factory
from api.routers import auth as auth_router
from api.services import auth as auth_service


async def _fake_get_secret(key: str) -> str:
    return "test-secret"


@pytest.fixture(autouse=True)
def _patch_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "client-id-123")
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    monkeypatch.setattr(auth_service, "get_secret", _fake_get_secret)
    monkeypatch.setattr(auth_router, "get_secret", _fake_get_secret)


async def test_login_redirects_to_github_with_matching_state_cookie(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/login", follow_redirects=False)

    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert location.netloc == "github.com"
    query = parse_qs(location.query)
    assert query["client_id"] == ["client-id-123"]

    csrf_cookie = response.cookies.get("oauth_state")
    assert csrf_cookie is not None
    assert csrf_cookie == query["state"][0]


@respx.mock
async def test_callback_creates_user_and_sets_session_cookie(client: AsyncClient) -> None:
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=Response(200, json={"access_token": "gho_test"})
    )
    respx.get("https://api.github.com/user").mock(
        return_value=Response(200, json={"login": "octocat", "email": "octocat@example.com"})
    )

    client.cookies.set("oauth_state", "csrf-abc")
    response = await client.get(
        "/api/v1/auth/callback",
        params={"code": "abc123", "state": "csrf-abc"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:3000"
    assert "session" in response.cookies

    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.github_login == "octocat"))
        user = result.scalar_one()
        assert user.email == "octocat@example.com"


@respx.mock
async def test_repeat_login_updates_existing_user_without_duplicating(client: AsyncClient) -> None:
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=Response(200, json={"access_token": "gho_test"})
    )
    respx.get("https://api.github.com/user").mock(
        return_value=Response(200, json={"login": "octocat", "email": "octocat@example.com"})
    )

    for i in range(2):
        client.cookies.set("oauth_state", f"csrf-{i}")
        response = await client.get(
            "/api/v1/auth/callback",
            params={"code": "abc123", "state": f"csrf-{i}"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.email == "octocat@example.com"))
        assert len(result.scalars().all()) == 1


@respx.mock
async def test_callback_falls_back_to_user_emails_when_email_private(client: AsyncClient) -> None:
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=Response(200, json={"access_token": "gho_test"})
    )
    respx.get("https://api.github.com/user").mock(
        return_value=Response(200, json={"login": "privateuser", "email": None})
    )
    respx.get("https://api.github.com/user/emails").mock(
        return_value=Response(
            200,
            json=[
                {"email": "secondary@example.com", "primary": False, "verified": True},
                {"email": "privateuser@users.noreply.github.com", "primary": True, "verified": True},
            ],
        )
    )

    client.cookies.set("oauth_state", "csrf-xyz")
    response = await client.get(
        "/api/v1/auth/callback",
        params={"code": "abc123", "state": "csrf-xyz"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "session" in response.cookies

    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.github_login == "privateuser"))
        user = result.scalar_one()
        assert user.email == "privateuser@users.noreply.github.com"


@respx.mock
async def test_callback_reports_missing_email_permission(client: AsyncClient) -> None:
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=Response(200, json={"access_token": "gho_test"})
    )
    respx.get("https://api.github.com/user").mock(
        return_value=Response(200, json={"login": "privateuser", "email": None})
    )
    respx.get("https://api.github.com/user/emails").mock(return_value=Response(403, json={}))

    client.cookies.set("oauth_state", "csrf-perm")
    response = await client.get(
        "/api/v1/auth/callback",
        params={"code": "abc123", "state": "csrf-perm"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "github_email_permission_missing"


@respx.mock
async def test_callback_reports_generic_github_error(client: AsyncClient) -> None:
    respx.post("https://github.com/login/oauth/access_token").mock(return_value=Response(500))

    client.cookies.set("oauth_state", "csrf-500")
    response = await client.get(
        "/api/v1/auth/callback",
        params={"code": "abc123", "state": "csrf-500"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "github_api_error"


async def test_callback_rejects_mismatched_state(client: AsyncClient) -> None:
    client.cookies.set("oauth_state", "expected")
    response = await client.get(
        "/api/v1/auth/callback",
        params={"code": "abc123", "state": "wrong"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_oauth_state"


async def test_callback_handles_access_denied(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/callback",
        params={"error": "access_denied", "state": "whatever"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "oauth_denied"


async def test_me_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_me_returns_current_user(client: AsyncClient) -> None:
    async with async_session_factory() as session:
        user = User(email="me@example.com", github_login="me")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    session_jwt = await auth_service.create_session_jwt(user.id)
    client.cookies.set("session", session_jwt)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user.id)
    assert body["email"] == "me@example.com"
    assert body["github_login"] == "me"


async def test_logout_clears_session_cookie(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert response.json()["status"] == "logged_out"
    set_cookie = response.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()
