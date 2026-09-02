import logging
import os

import jwt
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Repo, User
from api.db.session import get_db
from api.errors import ApiError
from api.services.auth import create_install_state_jwt, get_current_user, verify_install_state_jwt
from api.services.github_app import list_installation_repositories
from api.services.queue import enqueue_indexing_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])


@router.get("/install")
async def install(current_user: User = Depends(get_current_user)) -> RedirectResponse:
    """Starts the GitHub App installation flow for the logged-in user.

    `GET /callback` below verifies the `state` this generates via
    `verify_install_state_jwt` to know who owns the resulting installation_id.
    """
    state = await create_install_state_jwt(current_user.id)
    app_slug = os.environ["GITHUB_APP_SLUG"]
    return RedirectResponse(
        url=f"https://github.com/apps/{app_slug}/installations/new?state={state}",
        status_code=302,
    )


@router.get("/callback")
async def callback(
    installation_id: int | None = None,
    setup_action: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    frontend_url = os.environ["FRONTEND_URL"]

    if not state:
        raise ApiError(
            403, "missing_install_state", "Отсутствует state — невозможно определить владельца установки"
        )
    try:
        owner_id = await verify_install_state_jwt(state)
    except jwt.InvalidTokenError:
        raise ApiError(403, "invalid_install_state", "state недействителен или просрочен") from None

    if setup_action == "request":
        # Org member without admin rights requested the install — an org
        # admin still has to approve it, no installation exists yet.
        return RedirectResponse(url=f"{frontend_url}?install_status=pending_approval", status_code=302)

    if installation_id is None:
        raise ApiError(400, "missing_installation_id", "GitHub не передал installation_id")

    repos_data = await list_installation_repositories(installation_id)
    for repo_data in repos_data:
        result = await db.execute(
            select(Repo).where(Repo.owner_id == owner_id, Repo.repo_full_name == repo_data["full_name"])
        )
        repo = result.scalar_one_or_none()
        if repo is None:
            repo = Repo(
                owner_id=owner_id,
                installation_id=installation_id,
                repo_full_name=repo_data["full_name"],
                default_branch=repo_data.get("default_branch", "main"),
            )
            db.add(repo)
        else:
            repo.installation_id = installation_id
            repo.default_branch = repo_data.get("default_branch", "main")
        await db.flush()
        await enqueue_indexing_job(repo.id)

    await db.commit()

    return RedirectResponse(url=frontend_url, status_code=302)
