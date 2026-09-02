import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Repo
from api.db.session import get_db
from api.errors import ApiError
from api.services.queue import enqueue_incremental_indexing_job, enqueue_indexing_job
from api.services.secrets import get_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

ZERO_SHA = "0" * 40


class WebhookResponse(BaseModel):
    status: str


@router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    body = await request.body()
    await _verify_signature(body, x_hub_signature_256)

    if x_github_event != "push":
        return WebhookResponse(status="ignored")

    payload = json.loads(body)
    repo_full_name = payload.get("repository", {}).get("full_name")
    installation_id = payload.get("installation", {}).get("id")
    ref = payload.get("ref", "")
    after_sha = payload.get("after")

    if not repo_full_name or installation_id is None:
        return WebhookResponse(status="ignored")

    result = await db.execute(
        select(Repo).where(Repo.repo_full_name == repo_full_name, Repo.installation_id == installation_id)
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        logger.info("push webhook for untracked repo %s, ignoring", repo_full_name)
        return WebhookResponse(status="ignored")

    if ref != f"refs/heads/{repo.default_branch}":
        return WebhookResponse(status="ignored")

    if not after_sha or after_sha == ZERO_SHA:
        return WebhookResponse(status="ignored")  # branch deleted

    if repo.last_indexed_sha is None:
        await enqueue_indexing_job(repo.id)
    else:
        await enqueue_incremental_indexing_job(repo.id, repo.last_indexed_sha, after_sha)

    return WebhookResponse(status="accepted")


async def _verify_signature(body: bytes, signature_header: str | None) -> None:
    if not signature_header or not signature_header.startswith("sha256="):
        raise ApiError(401, "invalid_webhook_signature", "Отсутствует или некорректна подпись")

    secret = await get_secret("GITHUB_APP_WEBHOOK_SECRET")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise ApiError(401, "invalid_webhook_signature", "Подпись не совпадает")
