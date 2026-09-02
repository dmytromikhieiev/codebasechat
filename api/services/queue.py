import asyncio
import os
import uuid

from redis import Redis
from rq import Queue

INDEXING_QUEUE_NAME = "indexing"


def _get_queue() -> Queue:
    redis_conn = Redis.from_url(os.environ["REDIS_URL"])
    return Queue(INDEXING_QUEUE_NAME, connection=redis_conn)


def _enqueue_sync(repo_id: str) -> None:
    # worker.indexer is implemented in a later task (see PLAN.md) — enqueuing
    # by dotted path doesn't require it to exist yet, only for the worker to
    # eventually pick the job up.
    _get_queue().enqueue("worker.indexer.run_indexing_job", repo_id)


def _enqueue_incremental_sync(repo_id: str, base_sha: str, head_sha: str) -> None:
    _get_queue().enqueue("worker.indexer.run_incremental_indexing_job", repo_id, base_sha, head_sha)


async def enqueue_indexing_job(repo_id: uuid.UUID) -> None:
    await asyncio.to_thread(_enqueue_sync, str(repo_id))


async def enqueue_incremental_indexing_job(repo_id: uuid.UUID, base_sha: str, head_sha: str) -> None:
    await asyncio.to_thread(_enqueue_incremental_sync, str(repo_id), base_sha, head_sha)
