import logging
import os
import time

import httpx
import jwt

from api.services.secrets import get_github_app_private_key

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
APP_JWT_TTL_SECONDS = 600  # GitHub requires exp <= 10 minutes from now
CLOCK_DRIFT_ALLOWANCE_SECONDS = 60


async def generate_app_jwt() -> str:
    app_id = os.environ["GITHUB_APP_ID"]
    private_key = await get_github_app_private_key()
    now = int(time.time())
    payload = {
        "iat": now - CLOCK_DRIFT_ALLOWANCE_SECONDS,
        "exp": now + APP_JWT_TTL_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    app_jwt = await generate_app_jwt()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers=_app_jwt_headers(app_jwt),
        )
    response.raise_for_status()
    token = response.json()["token"]
    logger.info("obtained installation token for installation_id=%s (...%s)", installation_id, token[-4:])
    return token


async def list_app_installations() -> list[dict]:
    app_jwt = await generate_app_jwt()
    installations: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            response = await client.get(
                f"{GITHUB_API_BASE}/app/installations",
                headers=_app_jwt_headers(app_jwt),
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            installations.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    return installations


async def list_installation_repositories(installation_id: int) -> list[dict]:
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/installation/repositories",
            headers=_installation_token_headers(token),
        )
    response.raise_for_status()
    return response.json()["repositories"]


async def resolve_ref_to_sha(installation_id: int, repo_full_name: str, ref: str) -> str:
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/commits/{ref}",
            headers=_installation_token_headers(token),
        )
    response.raise_for_status()
    return response.json()["sha"]


async def compare_commits(installation_id: int, repo_full_name: str, base: str, head: str) -> list[dict]:
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/compare/{base}...{head}",
            headers=_installation_token_headers(token),
        )
    response.raise_for_status()
    return response.json().get("files", [])


def _app_jwt_headers(app_jwt: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _installation_token_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
