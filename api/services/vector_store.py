import dataclasses
import os
import uuid

from qdrant_client import AsyncQdrantClient, models

# voyage-code-3 default output dimensionality
QDRANT_VECTOR_SIZE = 1024
DISTANCE = models.Distance.COSINE


@dataclasses.dataclass
class VectorPoint:
    id: uuid.UUID
    vector: list[float]
    file_path: str
    start_line: int
    end_line: int
    function_name: str | None


def _client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=os.environ["QDRANT_URL"])


def _collection_name(repo_id: uuid.UUID) -> str:
    return f"repo_{repo_id}"


async def ensure_collection(repo_id: uuid.UUID) -> None:
    client = _client()
    collection_name = _collection_name(repo_id)
    if not await client.collection_exists(collection_name):
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(size=QDRANT_VECTOR_SIZE, distance=DISTANCE),
        )


async def search(repo_id: uuid.UUID, query_vector: list[float], limit: int) -> list[models.ScoredPoint]:
    client = _client()
    collection_name = _collection_name(repo_id)
    if not await client.collection_exists(collection_name):
        return []
    return await client.search(collection_name=collection_name, query_vector=query_vector, limit=limit)


async def delete_points_by_file_path(repo_id: uuid.UUID, file_path: str) -> None:
    client = _client()
    collection_name = _collection_name(repo_id)
    if not await client.collection_exists(collection_name):
        return
    await client.delete(
        collection_name=collection_name,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path))]
            )
        ),
    )


async def upsert_points(repo_id: uuid.UUID, points: list[VectorPoint]) -> None:
    client = _client()
    await client.upsert(
        collection_name=_collection_name(repo_id),
        points=[
            models.PointStruct(
                id=str(point.id),
                vector=point.vector,
                payload={
                    "file_path": point.file_path,
                    "start_line": point.start_line,
                    "end_line": point.end_line,
                    "function_name": point.function_name,
                },
            )
            for point in points
        ],
    )
