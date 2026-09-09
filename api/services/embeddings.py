import asyncio
import logging
from collections.abc import Iterator

import httpx

from api.services.secrets import get_secret

logger = logging.getLogger(__name__)

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_MODEL = "voyage-code-3"
MAX_BATCH_SIZE = 128
MAX_RETRIES = 5
RETRY_BASE_DELAY_SECONDS = 2.0


async def embed_documents(texts: list[str]) -> list[list[float]]:
    return await _embed(texts, input_type="document")


async def embed_query(text: str) -> list[float]:
    embeddings = await _embed([text], input_type="query")
    return embeddings[0]


async def _embed(texts: list[str], *, input_type: str) -> list[list[float]]:
    api_key = await get_secret("VOYAGE_API_KEY")
    embeddings: list[list[float]] = []
    for batch in _batched(texts, MAX_BATCH_SIZE):
        response = await _post_with_retry(api_key, batch, input_type)
        embeddings.extend(item["embedding"] for item in response.json()["data"])
    return embeddings


async def _post_with_retry(api_key: str, batch: list[str], input_type: str) -> httpx.Response:
    for attempt in range(MAX_RETRIES + 1):
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                VOYAGE_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": EMBEDDING_MODEL, "input": batch, "input_type": input_type},
            )

        # 429 (rate limit) and 5xx are transient — worth retrying with
        # backoff. Anything else (bad request, auth) won't fix itself.
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == MAX_RETRIES:
                response.raise_for_status()
            delay = _retry_delay(response, attempt)
            logger.warning(
                "Voyage API %s, retrying in %.1fs (attempt %d/%d)",
                response.status_code,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            continue

        response.raise_for_status()
        return response

    # unreachable — the loop above always returns or raises
    raise AssertionError("unreachable")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return RETRY_BASE_DELAY_SECONDS * (2**attempt)


def _batched(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
