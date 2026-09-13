import dataclasses
import json
import os
from abc import ABC, abstractmethod

import httpx
import openai

from api.services.retrieval import RetrievedChunk
from api.services.secrets import get_secret

VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"

DEFAULT_MODELS = {
    "voyage": "rerank-2",
    # OpenAI has no dedicated rerank endpoint (unlike Voyage) — a cheap chat
    # model scoring relevance via structured output stands in for one.
    "openai": "gpt-4o-mini",
}

RERANK_SYSTEM_PROMPT = (
    "You rank code snippets by how well they answer a question. Given a "
    "question and a numbered list of code snippets, score every snippet's "
    "relevance to the question from 0 (irrelevant) to 1 (directly answers "
    "it). Include every snippet index exactly once."
)

_RERANK_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "relevance_score": {"type": "number"},
                },
                "required": ["index", "relevance_score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


class RerankProvider(ABC):
    """One implementation per reranking backend. Which one is active is a
    config choice (see get_rerank_provider), not something callers pick."""

    @abstractmethod
    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        """Returns the top_k chunks, reordered by relevance, with .score set."""


class VoyageRerankProvider(RerankProvider):
    def __init__(self, model: str) -> None:
        self._model = model

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        api_key = await get_secret("VOYAGE_API_KEY")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                VOYAGE_RERANK_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": self._model,
                    "query": query,
                    "documents": [chunk.content for chunk in chunks],
                    "top_k": top_k,
                },
            )
        response.raise_for_status()

        return [
            dataclasses.replace(chunks[item["index"]], score=item["relevance_score"])
            for item in response.json()["data"]
        ]


class OpenAIRerankProvider(RerankProvider):
    def __init__(self, model: str) -> None:
        self._model = model

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        api_key = await get_secret("OPENAI_API_KEY")
        client = openai.AsyncOpenAI(api_key=api_key)

        snippets = "\n\n".join(f"[{i}] {chunk.content}" for i, chunk in enumerate(chunks))
        response = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": f"Question: {query}\n\nSnippets:\n{snippets}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "rerank_results", "strict": True, "schema": _RERANK_RESULT_SCHEMA},
            },
        )
        payload = json.loads(response.choices[0].message.content)
        results = sorted(payload["results"], key=lambda item: item["relevance_score"], reverse=True)

        reranked = []
        for item in results[:top_k]:
            index = item["index"]
            if 0 <= index < len(chunks):
                reranked.append(dataclasses.replace(chunks[index], score=item["relevance_score"]))
        return reranked


_PROVIDERS: dict[str, type[RerankProvider]] = {
    "voyage": VoyageRerankProvider,
    "openai": OpenAIRerankProvider,
}


def get_rerank_provider() -> RerankProvider:
    provider_name = os.environ.get("RERANK_PROVIDER", "voyage").lower()
    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ValueError(f"Unknown RERANK_PROVIDER {provider_name!r}, expected one of {sorted(_PROVIDERS)}")

    model = os.environ.get("RERANK_MODEL") or DEFAULT_MODELS[provider_name]
    return provider_cls(model)


async def rerank(query: str, chunks: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
    if not chunks:
        return []

    provider = get_rerank_provider()
    return await provider.rerank(query, chunks, top_k)
