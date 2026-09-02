import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import IndexingJob, Repo, User
from api.db.session import get_db
from api.errors import ApiError
from api.services.auth import get_current_user
from api.services.github_app import resolve_ref_to_sha
from api.services.queue import enqueue_incremental_indexing_job, enqueue_indexing_job

router = APIRouter(prefix="/api/v1/repos", tags=["repos"])


class RepoResponse(BaseModel):
    id: uuid.UUID
    repo_full_name: str
    default_branch: str
    status: str
    last_indexed_sha: str | None
    indexed_at: datetime | None
    created_at: datetime


class RepoDetailResponse(RepoResponse):
    progress: float | None
    files_done: int | None
    files_total: int | None
    error: str | None


class ReindexResponse(BaseModel):
    status: Literal["queued_full", "queued_incremental", "up_to_date"]


def _to_repo_response(repo: Repo) -> RepoResponse:
    return RepoResponse(
        id=repo.id,
        repo_full_name=repo.repo_full_name,
        default_branch=repo.default_branch,
        status=repo.status,
        last_indexed_sha=repo.last_indexed_sha,
        indexed_at=repo.indexed_at,
        created_at=repo.created_at,
    )


@router.get("", response_model=list[RepoResponse])
async def list_repos(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RepoResponse]:
    result = await db.execute(
        select(Repo).where(Repo.owner_id == current_user.id).order_by(Repo.created_at.desc())
    )
    return [_to_repo_response(repo) for repo in result.scalars().all()]


@router.get("/{repo_id}", response_model=RepoDetailResponse)
async def get_repo(
    repo_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RepoDetailResponse:
    repo = await db.get(Repo, repo_id)
    if repo is None or repo.owner_id != current_user.id:
        raise ApiError(404, "repo_not_found", "Репозиторий не найден")

    job_result = await db.execute(
        select(IndexingJob).where(IndexingJob.repo_id == repo_id).order_by(IndexingJob.created_at.desc()).limit(1)
    )
    latest_job = job_result.scalar_one_or_none()

    return RepoDetailResponse(
        **_to_repo_response(repo).model_dump(),
        progress=latest_job.progress if latest_job else None,
        files_done=latest_job.files_done if latest_job else None,
        files_total=latest_job.files_total if latest_job else None,
        error=latest_job.error if latest_job else None,
    )


@router.post("/{repo_id}/reindex", response_model=ReindexResponse)
async def reindex(
    repo_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReindexResponse:
    """Manual substitute for the push webhook (see webhooks.md) — same
    incremental-vs-full decision, triggered by the user instead of GitHub.
    """
    repo = await db.get(Repo, repo_id)
    if repo is None or repo.owner_id != current_user.id:
        raise ApiError(404, "repo_not_found", "Репозиторий не найден")
    if repo.status == "indexing":
        raise ApiError(409, "already_indexing", "Индексация уже выполняется")

    if repo.last_indexed_sha is None:
        await enqueue_indexing_job(repo.id)
        return ReindexResponse(status="queued_full")

    head_sha = await resolve_ref_to_sha(repo.installation_id, repo.repo_full_name, repo.default_branch)
    if head_sha == repo.last_indexed_sha:
        return ReindexResponse(status="up_to_date")

    await enqueue_incremental_indexing_job(repo.id, repo.last_indexed_sha, head_sha)
    return ReindexResponse(status="queued_incremental")
