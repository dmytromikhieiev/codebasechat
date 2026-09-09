import uuid

import pytest

from api.errors import ApiError
from api.services import rate_limit


@pytest.fixture(autouse=True)
async def _cleanup_keys():
    yield
    async for key in rate_limit._client().scan_iter("ratelimit:ask:test-*"):
        await rate_limit._client().delete(key)


async def test_allows_requests_up_to_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 2)
    repo_id = "test-repo-allow"

    await rate_limit.enforce_ask_rate_limit(repo_id)
    await rate_limit.enforce_ask_rate_limit(repo_id)  # exactly at the limit — still allowed


async def test_blocks_requests_over_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 2)
    repo_id = "test-repo-block"

    await rate_limit.enforce_ask_rate_limit(repo_id)
    await rate_limit.enforce_ask_rate_limit(repo_id)
    with pytest.raises(ApiError) as exc_info:
        await rate_limit.enforce_ask_rate_limit(repo_id)

    assert exc_info.value.status_code == 429
    assert exc_info.value.error == "rate_limited"


async def test_limit_is_scoped_per_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)

    await rate_limit.enforce_ask_rate_limit("test-repo-a")
    await rate_limit.enforce_ask_rate_limit("test-repo-b")  # different repo, independent counter


async def test_accepts_uuid_repo_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 5)
    repo_id = uuid.uuid4()

    await rate_limit.enforce_ask_rate_limit(repo_id)

    key = f"ratelimit:ask:{repo_id}"
    assert await rate_limit._client().get(key) is not None
    await rate_limit._client().delete(key)
