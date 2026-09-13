import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator

import httpx
import openai
import tiktoken

from api.services.secrets import get_secret
from api.services.vector_store import QDRANT_VECTOR_SIZE

logger = logging.getLogger(__name__)

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
MAX_BATCH_SIZE = 128
MAX_RETRIES = 5
RETRY_BASE_DELAY_SECONDS = 2.0

# text-embedding-3-*'s hard input cap (Voyage's voyage-code-3 allows 32000
# tokens per input, so oversized chunks only surface here) — over-long
# chunks (a large generated file, a huge minified bundle) are truncated
# rather than failing the whole batch.
OPENAI_MAX_INPUT_TOKENS = 8192

DEFAULT_MODELS = {
    "voyage": "voyage-code-3",
    "openai": "text-embedding-3-small",
}


class EmbeddingProvider(ABC):
    """One implementation per embedding backend. Which one is active is a
    config choice (see get_embedding_provider), not something callers pick.

    Switching provider on an already-indexed repo isn't just a config flip:
    both providers are made to output QDRANT_VECTOR_SIZE-dim vectors so the
    Qdrant collection schema stays compatible, but the two providers' vector
    spaces are otherwise unrelated — cosine similarity between an old
    (pre-switch) vector and a new one is meaningless. A full reindex
    (`recreate_collection`) is required after switching.
    """

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Vectors for indexing, one per text, order preserved."""

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """Vector for a single search query."""


class VoyageEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model: str) -> None:
        self._model = model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, input_type="document")

    async def embed_query(self, text: str) -> list[float]:
        embeddings = await self._embed([text], input_type="query")
        return embeddings[0]

    async def _embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        api_key = await get_secret("VOYAGE_API_KEY")
        embeddings: list[list[float]] = []
        for batch in _batched(texts, MAX_BATCH_SIZE):
            response = await self._post_with_retry(api_key, batch, input_type)
            embeddings.extend(item["embedding"] for item in response.json()["data"])
        return embeddings

    async def _post_with_retry(self, api_key: str, batch: list[str], input_type: str) -> httpx.Response:
        for attempt in range(MAX_RETRIES + 1):
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    VOYAGE_API_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": self._model, "input": batch, "input_type": input_type},
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


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model: str) -> None:
        self._model = model
        self._encoding = tiktoken.get_encoding("cl100k_base")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        embeddings = await self._embed([text])
        return embeddings[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        api_key = await get_secret("OPENAI_API_KEY")
        client = openai.AsyncOpenAI(api_key=api_key)
        truncated = [self._truncate(text) for text in texts]
        embeddings: list[list[float]] = []
        for batch in _batched(truncated, MAX_BATCH_SIZE):
            # `dimensions` truncates text-embedding-3-*'s native output so it
            # matches QDRANT_VECTOR_SIZE regardless of which provider is
            # active — keeps the Qdrant collection schema provider-agnostic.
            response = await client.embeddings.create(model=self._model, input=batch, dimensions=QDRANT_VECTOR_SIZE)
            embeddings.extend(item.embedding for item in response.data)
        return embeddings

    def _truncate(self, text: str) -> str:
        tokens = self._encoding.encode(text)
        if len(tokens) <= OPENAI_MAX_INPUT_TOKENS:
            return text
        logger.warning(
            "Chunk is %d tokens, truncating to %d for OpenAI embeddings", len(tokens), OPENAI_MAX_INPUT_TOKENS
        )
        return self._encoding.decode(tokens[:OPENAI_MAX_INPUT_TOKENS])


_PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    "voyage": VoyageEmbeddingProvider,
    "openai": OpenAIEmbeddingProvider,
}


def get_embedding_provider() -> EmbeddingProvider:
    provider_name = os.environ.get("EMBEDDING_PROVIDER", "voyage").lower()
    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ValueError(f"Unknown EMBEDDING_PROVIDER {provider_name!r}, expected one of {sorted(_PROVIDERS)}")

    model = os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODELS[provider_name]
    return provider_cls(model)


async def embed_documents(texts: list[str]) -> list[list[float]]:
    return await get_embedding_provider().embed_documents(texts)


async def embed_query(text: str) -> list[float]:
    return await get_embedding_provider().embed_query(text)


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
