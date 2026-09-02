import uuid

import pytest

from api.services import vector_store


@pytest.fixture
async def repo_id():
    repo_id = uuid.uuid4()
    yield repo_id
    client = vector_store._client()
    if await client.collection_exists(vector_store._collection_name(repo_id)):
        await client.delete_collection(vector_store._collection_name(repo_id))


async def test_ensure_collection_is_idempotent(repo_id: uuid.UUID) -> None:
    await vector_store.ensure_collection(repo_id)
    await vector_store.ensure_collection(repo_id)  # must not raise on second call

    client = vector_store._client()
    assert await client.collection_exists(vector_store._collection_name(repo_id))


async def test_upsert_points_roundtrip(repo_id: uuid.UUID) -> None:
    await vector_store.ensure_collection(repo_id)
    point_id = uuid.uuid4()

    await vector_store.upsert_points(
        repo_id,
        [
            vector_store.VectorPoint(
                id=point_id,
                vector=[0.1] * vector_store.QDRANT_VECTOR_SIZE,
                file_path="a.py",
                start_line=1,
                end_line=2,
                function_name="foo",
            )
        ],
    )

    client = vector_store._client()
    points = await client.retrieve(collection_name=vector_store._collection_name(repo_id), ids=[str(point_id)])

    assert len(points) == 1
    assert points[0].payload["file_path"] == "a.py"
    assert points[0].payload["function_name"] == "foo"


async def test_delete_points_by_file_path_removes_only_matching_points(repo_id: uuid.UUID) -> None:
    await vector_store.ensure_collection(repo_id)
    keep_id = uuid.uuid4()
    remove_id = uuid.uuid4()

    await vector_store.upsert_points(
        repo_id,
        [
            vector_store.VectorPoint(
                id=keep_id, vector=[0.1] * vector_store.QDRANT_VECTOR_SIZE,
                file_path="keep.py", start_line=1, end_line=1, function_name=None,
            ),
            vector_store.VectorPoint(
                id=remove_id, vector=[0.2] * vector_store.QDRANT_VECTOR_SIZE,
                file_path="remove.py", start_line=1, end_line=1, function_name=None,
            ),
        ],
    )

    await vector_store.delete_points_by_file_path(repo_id, "remove.py")

    client = vector_store._client()
    points = await client.retrieve(
        collection_name=vector_store._collection_name(repo_id), ids=[str(keep_id), str(remove_id)]
    )
    assert {p.id for p in points} == {str(keep_id)}


async def test_delete_points_by_file_path_on_missing_collection_is_noop() -> None:
    await vector_store.delete_points_by_file_path(uuid.uuid4(), "whatever.py")
