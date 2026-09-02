import os
import uuid

import redis.asyncio as redis

from api.errors import ApiError

RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60


def _client() -> redis.Redis:
    # A fresh client per call, not a persistent module-level one: a pooled
    # connection is bound to the event loop it was first used on, which
    # breaks the moment it's reused from a different loop (e.g. one test
    # per event loop under pytest-asyncio).
    return redis.from_url(os.environ["REDIS_URL"])


async def enforce_ask_rate_limit(repo_id: uuid.UUID | str) -> None:
    client = _client()
    try:
        key = f"ratelimit:ask:{repo_id}"
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    finally:
        await client.aclose()

    if count > RATE_LIMIT_MAX_REQUESTS:
        raise ApiError(429, "rate_limited", "Слишком много запросов к этому репозиторию, попробуйте позже")
