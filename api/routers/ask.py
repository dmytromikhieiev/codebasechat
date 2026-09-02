import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Query as QueryRow
from api.db.models import Repo, User
from api.db.session import get_db
from api.errors import ApiError
from api.services.answer import stream_answer
from api.services.auth import get_current_user
from api.services.rate_limit import enforce_ask_rate_limit
from api.services.reranker import rerank
from api.services.retrieval import RetrievedChunk, hybrid_search

router = APIRouter(prefix="/api/v1/repos", tags=["ask"])

RERANK_TOP_K = 5


class AskRequest(BaseModel):
    question: str


@router.post("/{repo_id}/ask")
async def ask(
    repo_id: uuid.UUID,
    body: AskRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    repo = await db.get(Repo, repo_id)
    if repo is None or repo.owner_id != current_user.id:
        raise ApiError(404, "repo_not_found", "Репозиторий не найден")
    if repo.status != "ready":
        raise ApiError(409, "repo_not_ready", "Репозиторий ещё не проиндексирован")

    await enforce_ask_rate_limit(repo_id)

    candidates = await hybrid_search(db, repo_id, body.question)
    top_chunks = await rerank(body.question, candidates, top_k=RERANK_TOP_K)

    return StreamingResponse(
        _stream_events(db, repo_id, current_user.id, body.question, top_chunks),
        media_type="text/event-stream",
    )


async def _stream_events(
    db: AsyncSession,
    repo_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    chunks: list[RetrievedChunk],
) -> AsyncIterator[str]:
    started_at = time.monotonic()

    sources = [
        {
            "file_path": chunk.file_path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "function_name": chunk.function_name,
        }
        for chunk in chunks
    ]
    yield _sse_event("sources", {"sources": sources})

    answer_parts: list[str] = []
    async for text in stream_answer(question, chunks):
        answer_parts.append(text)
        yield _sse_event("token", {"text": text})

    latency_ms = int((time.monotonic() - started_at) * 1000)
    query = QueryRow(
        repo_id=repo_id,
        user_id=user_id,
        question=question,
        answer="".join(answer_parts),
        retrieved_chunk_ids=[{"chunk_id": str(chunk.id), "score": chunk.score} for chunk in chunks],
        latency_ms=latency_ms,
    )
    db.add(query)
    await db.commit()
    await db.refresh(query)

    yield _sse_event("done", {"query_id": str(query.id)})


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
