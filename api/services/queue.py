import asyncio
import os
import uuid

from redis import Redis
from rq import Queue

INDEXING_QUEUE_NAME = "indexing"

# RQ's default job_timeout (180s) is sized for short background tasks, not a
# full-repo reindex — that's a loop of one embedding-API round trip per file,
# which routinely runs past it on anything but a tiny repo (RQ kills the job
# with JobTimeoutException once the default is hit).
INDEXING_JOB_TIMEOUT_SECONDS = 3600


def _get_queue() -> Queue:
    redis_conn = Redis.from_url(os.environ["REDIS_URL"])
    return Queue(INDEXING_QUEUE_NAME, connection=redis_conn)


def _enqueue_sync(repo_id: str) -> None:
    # worker.indexer is implemented in a later task (see PLAN.md) — enqueuing
    # by dotted path doesn't require it to exist yet, only for the worker to
    # eventually pick the job up.
    _get_queue().enqueue("worker.indexer.run_indexing_job", repo_id, job_timeout=INDEXING_JOB_TIMEOUT_SECONDS)


def _enqueue_incremental_sync(repo_id: str, base_sha: str, head_sha: str) -> None:
    _get_queue().enqueue(
        "worker.indexer.run_incremental_indexing_job",
        repo_id,
        base_sha,
        head_sha,
        job_timeout=INDEXING_JOB_TIMEOUT_SECONDS,
    )


async def enqueue_indexing_job(repo_id: uuid.UUID) -> None:
    await asyncio.to_thread(_enqueue_sync, str(repo_id))


async def enqueue_incremental_indexing_job(repo_id: uuid.UUID, base_sha: str, head_sha: str) -> None:
    await asyncio.to_thread(_enqueue_incremental_sync, str(repo_id), base_sha, head_sha)
