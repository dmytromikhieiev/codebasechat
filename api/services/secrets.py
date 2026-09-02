import asyncio
import os
from pathlib import Path

import httpx

VAULT_SECRET_PATH = "secret/data/rag-codebase-chat"


async def get_secret(key: str) -> str:
    """Vault-backed secret lookup. Falls back to environment variables when APP_ENV=local."""
    if os.environ.get("APP_ENV") == "local":
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"secret {key!r} is not set")
        return value
    return await _get_secret_from_vault(key)


async def get_github_app_private_key() -> str:
    """PEM key material, kept separate from get_secret() since it's not a
    single short env value: locally it lives in a file (GITHUB_APP_PRIVATE_KEY_PATH
    points at it), in Vault it's stored as its own entry.
    """
    if os.environ.get("APP_ENV") == "local":
        path = os.environ["GITHUB_APP_PRIVATE_KEY_PATH"]
        return await asyncio.to_thread(Path(path).read_text)
    return await _get_secret_from_vault("GITHUB_APP_PRIVATE_KEY")


async def _get_secret_from_vault(key: str) -> str:
    vault_addr = os.environ["VAULT_ADDR"]
    vault_token = os.environ["VAULT_TOKEN"]

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            f"{vault_addr}/v1/{VAULT_SECRET_PATH}",
            headers={"X-Vault-Token": vault_token},
        )
    response.raise_for_status()
    data = response.json()["data"]["data"]
    if key not in data:
        raise KeyError(f"secret {key!r} not found in vault")
    return str(data[key])
