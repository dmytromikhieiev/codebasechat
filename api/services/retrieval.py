import dataclasses
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Chunk as ChunkRow
from api.services import vector_store
from api.services.embeddings import embed_query

BM25_TOP_K = 20
VECTOR_TOP_K = 20
HYBRID_TOP_K = 20
RRF_K = 60

# file_path/function_name matches outrank a plain content match — a question
# naming a file/symbol should surface it even if another file mentions the
# same words in prose (see .claude/tasks/pg-search-bm25.md). `lenient` on
# every parse_with_field call keeps a natural-language question (parens,
# apostrophes, unbalanced quotes) from raising a Tantivy query-parse error;
# it doesn't suppress capitalized AND/OR being read as boolean operators
# (verified interactively — a rare, acceptable false-negative for MVP).
FILE_PATH_BOOST = 3.0
FUNCTION_NAME_BOOST = 2.0

_BM25_QUERY = text(
    """
    SELECT embedding_id
    FROM chunks
    WHERE repo_id = :repo_id
      AND id @@@ paradedb.boolean(
            should => ARRAY[
                paradedb.boost(
                    factor => :file_path_boost,
                    query => paradedb.parse_with_field('file_path', :query, lenient => true)
                ),
                paradedb.boost(
                    factor => :function_name_boost,
                    query => paradedb.parse_with_field('function_name', :query, lenient => true)
                ),
                paradedb.parse_with_field('content', :query, lenient => true)
            ]
        )
    ORDER BY paradedb.score(id) DESC
    LIMIT :limit
    """
)

# A question naming a specific file (e.g. "explain docker-compose.prod.yml")
# can lose to other files in the generic top-K fusion above — another file
# mentioning the same words in prose can easily outscore it. Capped so one
# pinned file can't by itself blow the answer model's context/rate budget
# (see OPENAI_MAX_INPUT_TOKENS in embeddings.py for the same class of guard).
MAX_PINNED_FILE_CHUNKS = 5


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


async def find_mentioned_file_chunks(db: AsyncSession, repo_id: uuid.UUID, question: str) -> list[RetrievedChunk]:
    """If the question names one of the repo's actual indexed files (by
    basename), return that file's chunks in full (capped), so it can be
    guaranteed a spot in the answer's context instead of merely competing
    for a rerank slot against unrelated files that happen to score higher.
    """
    file_paths = await _list_indexed_file_paths(db, repo_id)
    matched = _find_mentioned_file_path(question, file_paths)
    if matched is None:
        return []
    return await _load_file_chunks(db, repo_id, matched, MAX_PINNED_FILE_CHUNKS)


def _find_mentioned_file_path(question: str, file_paths: list[str]) -> str | None:
    question_lower = question.lower()
    matches = [path for path in file_paths if _basename(path).lower() in question_lower]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # Same basename in multiple directories (monorepo-style) — prefer a
    # match whose full path also appears in the question; failing that,
    # the shallowest path is the least arbitrary guess.
    full_path_matches = [path for path in matches if path.lower() in question_lower]
    candidates = full_path_matches or matches
    return min(candidates, key=lambda path: path.count("/"))


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


async def _list_indexed_file_paths(db: AsyncSession, repo_id: uuid.UUID) -> list[str]:
    stmt = select(ChunkRow.file_path).where(ChunkRow.repo_id == repo_id).distinct()
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_file_chunks(db: AsyncSession, repo_id: uuid.UUID, file_path: str, limit: int) -> list[RetrievedChunk]:
    stmt = (
        select(ChunkRow)
        .where(ChunkRow.repo_id == repo_id, ChunkRow.file_path == file_path)
        .order_by(ChunkRow.start_line)
        .limit(limit)
    )
    result = await db.execute(stmt)
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
        for row in result.scalars().all()
    ]


async def _bm25_search(db: AsyncSession, repo_id: uuid.UUID, query: str) -> list[uuid.UUID]:
    result = await db.execute(
        _BM25_QUERY,
        {
            "repo_id": repo_id,
            "query": query,
            "file_path_boost": FILE_PATH_BOOST,
            "function_name_boost": FUNCTION_NAME_BOOST,
            "limit": BM25_TOP_K,
        },
    )
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
