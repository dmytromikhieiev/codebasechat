import logging
import os
import uuid

import jwt
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Repo, User
from api.db.session import get_db
from api.errors import ApiError
from api.services.auth import create_install_state_jwt, get_current_user, verify_install_state_jwt
from api.services.github_app import list_app_installations, list_installation_repositories
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


async def _match_installations(github_login: str) -> list[dict]:
    installations = await list_app_installations()
    return [
        installation
        for installation in installations
        if installation.get("account", {}).get("login", "").lower() == github_login.lower()
    ]


async def _sync_installation_repos(
    db: AsyncSession,
    owner_id: uuid.UUID,
    installation_id: int,
    only_repo_full_name: str | None = None,
) -> list[Repo]:
    repos_data = await list_installation_repositories(installation_id)
    if only_repo_full_name is not None:
        repos_data = [r for r in repos_data if r["full_name"] == only_repo_full_name]

    synced: list[Repo] = []
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
        synced.append(repo)
    return synced


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

    await _sync_installation_repos(db, owner_id, installation_id)
    await db.commit()

    return RedirectResponse(url=frontend_url, status_code=302)


class AvailableRepoResponse(BaseModel):
    installation_id: int
    repo_full_name: str
    default_branch: str


@router.get("/available-repos", response_model=list[AvailableRepoResponse])
async def list_available_repos(current_user: User = Depends(get_current_user)) -> list[AvailableRepoResponse]:
    """Repos visible to this user's installation(s) of the app, for picking
    one to sync explicitly — see POST /sync's `repo_full_name`. Same
    installation-discovery path as /sync, just without writing anything.
    """
    if not current_user.github_login:
        raise ApiError(400, "missing_github_login", "У пользователя не привязан GitHub-логин")

    matching = await _match_installations(current_user.github_login)
    if not matching:
        raise ApiError(
            404,
            "installation_not_found",
            "Установка приложения для вашего аккаунта не найдена — убедитесь, что вы установили "
            "GitHub App (кнопка «Подключить репозиторий»)",
        )

    available: list[AvailableRepoResponse] = []
    for installation in matching:
        repos_data = await list_installation_repositories(installation["id"])
        available.extend(
            AvailableRepoResponse(
                installation_id=installation["id"],
                repo_full_name=repo_data["full_name"],
                default_branch=repo_data.get("default_branch", "main"),
            )
            for repo_data in repos_data
        )
    return available


class SyncRequest(BaseModel):
    repo_full_name: str | None = None


class SyncResponse(BaseModel):
    synced_repos: list[str]


@router.post("/sync", response_model=SyncResponse)
async def sync_installations(
    body: SyncRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyncResponse:
    """Manual fallback for GET /callback: finds this app's installation(s)
    for the current user's GitHub account directly via the App API
    (GET /app/installations, matched by account login) and syncs repos from
    there. Doesn't depend on GitHub's Setup URL redirect actually firing —
    useful when that's misconfigured, or when an org admin installed the
    app and this user never went through the install redirect themselves.

    With `repo_full_name` set, only that one repo is synced (see
    GET /available-repos to list candidates); omitted, every repo visible
    to the matching installation(s) is synced.
    """
    if not current_user.github_login:
        raise ApiError(400, "missing_github_login", "У пользователя не привязан GitHub-логин")

    repo_full_name = body.repo_full_name if body else None

    matching = await _match_installations(current_user.github_login)
    if not matching:
        raise ApiError(
            404,
            "installation_not_found",
            "Установка приложения для вашего аккаунта не найдена — убедитесь, что вы установили "
            "GitHub App (кнопка «Подключить репозиторий»)",
        )

    synced_repos: list[str] = []
    for installation in matching:
        synced = await _sync_installation_repos(db, current_user.id, installation["id"], repo_full_name)
        synced_repos.extend(repo.repo_full_name for repo in synced)

    if repo_full_name is not None and not synced_repos:
        raise ApiError(
            404,
            "repo_not_found_in_installation",
            f"Репозиторий {repo_full_name} не найден среди доступных установке приложения",
        )

    await db.commit()

    return SyncResponse(synced_repos=synced_repos)
