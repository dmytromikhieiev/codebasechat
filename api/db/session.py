import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# NullPool: a pooled asyncpg connection is bound to the event loop it was
# created on. Without this, tests that run each async test on its own loop
# (pytest-asyncio) crash with "attached to a different loop" once a pooled
# connection from an earlier test gets reused.
engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
