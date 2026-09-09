import pytest
from sqlalchemy import delete

from api.db.models import User
from api.db.session import async_session_factory


@pytest.fixture(autouse=True)
async def _clean_users_table():
    yield
    # users cascades to repos -> indexing_jobs/chunks per alembic/versions/0001_init.py,
    # so this alone clears everything created by a test.
    async with async_session_factory() as session:
        await session.execute(delete(User))
        await session.commit()
