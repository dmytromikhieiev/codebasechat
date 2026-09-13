import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete

_APP_DATABASE_URL = os.environ["DATABASE_URL"]
_TEST_DATABASE_URL = _APP_DATABASE_URL.rsplit("/", 1)[0] + "/rag_test"

# The cleanup fixture below truncates `users` (cascading to everything)
# after every test. Without this override it ran against the same database
# as the actual running app (DATABASE_URL as set in docker-compose.yml),
# which meant every `pytest` run silently wiped real dev data. Tests get
# their own database on the same Postgres instance instead — created and
# migrated to head below, before anything imports api.db.session (which
# builds its engine from this env var at import time).
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL


async def _ensure_test_database_exists() -> None:
    admin_dsn = _APP_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'rag_test'")
        if not exists:
            await conn.execute("CREATE DATABASE rag_test")
    finally:
        await conn.close()


asyncio.run(_ensure_test_database_exists())
command.upgrade(Config("alembic.ini"), "head")

from api.db.models import User  # noqa: E402
from api.db.session import async_session_factory  # noqa: E402


@pytest.fixture(autouse=True)
async def _clean_users_table():
    yield
    # users cascades to repos -> indexing_jobs/chunks per alembic/versions/0001_init.py,
    # so this alone clears everything created by a test.
    async with async_session_factory() as session:
        await session.execute(delete(User))
        await session.commit()
