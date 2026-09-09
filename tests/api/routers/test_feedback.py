import uuid

import pytest
from httpx import AsyncClient

from api.db.models import Query as QueryRow
from api.db.models import QueryFeedback, Repo, User
from api.db.session import async_session_factory
from api.services import auth


async def _login(client: AsyncClient, user_id: uuid.UUID) -> None:
    session_jwt = await auth.create_session_jwt(user_id)
    client.cookies.set("session", session_jwt)


@pytest.fixture
async def query_owned_by() -> tuple[User, QueryRow]:
    async with async_session_factory() as session:
        user = User(email="owner@example.com", github_login="owner")
        session.add(user)
        await session.flush()
        repo = Repo(
            owner_id=user.id, installation_id=1, repo_full_name="octocat/hello", default_branch="main", status="ready"
        )
        session.add(repo)
        await session.flush()
        query = QueryRow(repo_id=repo.id, user_id=user.id, question="what does foo do?", answer="it returns 1")
        session.add(query)
        await session.commit()
        await session.refresh(user)
        await session.refresh(query)
        return user, query


async def test_feedback_requires_auth(client: AsyncClient, query_owned_by) -> None:
    _, query = query_owned_by
    response = await client.post(f"/api/v1/queries/{query.id}/feedback", json={"rating": 1})

    assert response.status_code == 401


async def test_feedback_404_for_missing_query(client: AsyncClient, query_owned_by) -> None:
    user, _ = query_owned_by
    await _login(client, user.id)

    response = await client.post(f"/api/v1/queries/{uuid.uuid4()}/feedback", json={"rating": 1})

    assert response.status_code == 404


async def test_feedback_404_for_other_users_query(client: AsyncClient, query_owned_by) -> None:
    _, query = query_owned_by
    async with async_session_factory() as session:
        other_user = User(email="other@example.com", github_login="other")
        session.add(other_user)
        await session.commit()
        await session.refresh(other_user)

    await _login(client, other_user.id)
    response = await client.post(f"/api/v1/queries/{query.id}/feedback", json={"rating": 1})

    assert response.status_code == 404


async def test_feedback_rejects_invalid_rating(client: AsyncClient, query_owned_by) -> None:
    user, query = query_owned_by
    await _login(client, user.id)

    response = await client.post(f"/api/v1/queries/{query.id}/feedback", json={"rating": 0})

    assert response.status_code == 422


async def test_feedback_persists_rating_and_comment(client: AsyncClient, query_owned_by) -> None:
    user, query = query_owned_by
    await _login(client, user.id)

    response = await client.post(
        f"/api/v1/queries/{query.id}/feedback", json={"rating": -1, "comment": "wrong file"}
    )

    assert response.status_code == 200
    feedback_id = uuid.UUID(response.json()["id"])

    async with async_session_factory() as session:
        feedback = await session.get(QueryFeedback, feedback_id)
        assert feedback.query_id == query.id
        assert feedback.rating == -1
        assert feedback.comment == "wrong file"
