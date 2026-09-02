from collections.abc import Iterator

import httpx

from api.services.secrets import get_secret

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_MODEL = "voyage-code-3"
MAX_BATCH_SIZE = 128


async def embed_documents(texts: list[str]) -> list[list[float]]:
    return await _embed(texts, input_type="document")


async def embed_query(text: str) -> list[float]:
    embeddings = await _embed([text], input_type="query")
    return embeddings[0]


async def _embed(texts: list[str], *, input_type: str) -> list[list[float]]:
    api_key = await get_secret("VOYAGE_API_KEY")
    embeddings: list[list[float]] = []
    for batch in _batched(texts, MAX_BATCH_SIZE):
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                VOYAGE_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": EMBEDDING_MODEL, "input": batch, "input_type": input_type},
            )
        response.raise_for_status()
        embeddings.extend(item["embedding"] for item in response.json()["data"])
    return embeddings


def _batched(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
