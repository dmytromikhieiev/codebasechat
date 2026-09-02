import dataclasses
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Chunk as ChunkRow
from api.services import vector_store
from api.services.embeddings import embed_query

BM25_TOP_K = 20
VECTOR_TOP_K = 20
HYBRID_TOP_K = 20
RRF_K = 60


@dataclasses.dataclass
class RetrievedChunk:
    id: uuid.UUID
    file_path: str
    start_line: int
    end_line: int
    function_name: str | None
    language: str
    content: str
    score: float | None = None


async def hybrid_search(db: AsyncSession, repo_id: uuid.UUID, query: str) -> list[RetrievedChunk]:
    bm25_ids = await _bm25_search(db, repo_id, query)
    vector_ids = await _vector_search(repo_id, query)

    fused_ids = _reciprocal_rank_fusion([bm25_ids, vector_ids])[:HYBRID_TOP_K]

    return await _load_chunks(db, repo_id, fused_ids)


async def _bm25_search(db: AsyncSession, repo_id: uuid.UUID, query: str) -> list[uuid.UUID]:
    tsquery = func.plainto_tsquery("english", query)
    tsvector = func.to_tsvector("english", ChunkRow.content)
    stmt = (
        select(ChunkRow.embedding_id)
        .where(ChunkRow.repo_id == repo_id, tsvector.op("@@")(tsquery))
        .order_by(func.ts_rank(tsvector, tsquery).desc())
        .limit(BM25_TOP_K)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _vector_search(repo_id: uuid.UUID, query: str) -> list[uuid.UUID]:
    query_vector = await embed_query(query)
    points = await vector_store.search(repo_id, query_vector, limit=VECTOR_TOP_K)
    return [uuid.UUID(point.id) for point in points]


def _reciprocal_rank_fusion(id_lists: list[list[uuid.UUID]], k: int = RRF_K) -> list[uuid.UUID]:
    scores: dict[uuid.UUID, float] = {}
    for id_list in id_lists:
        for rank, item_id in enumerate(id_list, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item_id: scores[item_id], reverse=True)


async def _load_chunks(db: AsyncSession, repo_id: uuid.UUID, embedding_ids: list[uuid.UUID]) -> list[RetrievedChunk]:
    if not embedding_ids:
        return []
    stmt = select(ChunkRow).where(ChunkRow.repo_id == repo_id, ChunkRow.embedding_id.in_(embedding_ids))
    result = await db.execute(stmt)
    rows_by_embedding_id = {row.embedding_id: row for row in result.scalars().all()}
    return [
        RetrievedChunk(
            id=row.id,
            file_path=row.file_path,
            start_line=row.start_line,
            end_line=row.end_line,
            function_name=row.function_name,
            language=row.language,
            content=row.content,
        )
        for eid in embedding_ids
        if (row := rows_by_embedding_id.get(eid)) is not None
    ]
