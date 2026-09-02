import dataclasses

import httpx

from api.services.retrieval import RetrievedChunk
from api.services.secrets import get_secret

VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"
RERANK_MODEL = "rerank-2"


async def rerank(query: str, chunks: list[RetrievedChunk], top_k: int = 5) -> list[RetrievedChunk]:
    if not chunks:
        return []

    api_key = await get_secret("VOYAGE_API_KEY")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            VOYAGE_RERANK_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": RERANK_MODEL,
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
