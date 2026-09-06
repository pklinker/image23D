"""Test bootstrap.

These tests run against a real Postgres and a real Redis -- the same servers the
dev stack uses, but an isolated database (`image23d_test`) and an isolated Redis
db index, so nothing here can touch dev job rows or artifacts. The job state
machine is exactly the thing that was broken (PLAN-BUGFIX.md item 1), and
mocking the database away would mock away the bug.

The environment has to be set before anything imports `common.settings`, which
is why it happens at module scope here rather than in a fixture.
"""
import os

TEST_DB = os.environ.get("TEST_DB_NAME", "image23d_test")
PG_HOST = os.environ.get("TEST_PG_HOST", "localhost:5432")
PG_USER = os.environ.get("TEST_PG_USER", "image23d")
PG_PASS = os.environ.get("TEST_PG_PASSWORD", "image23d")

os.environ["DATABASE_URL"] = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}/{TEST_DB}"
# db 15, well away from the app's db 0.
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import common.db as common_db  # noqa: E402

# NullPool so no connection outlives the test that opened it. Swapped in before
# any module does `from common.db import SessionLocal` and binds the name.
common_db.engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
common_db.SessionLocal = async_sessionmaker(common_db.engine, expire_on_commit=False)

from common.models import Base  # noqa: E402


async def _create_database_if_missing() -> None:
    admin = await asyncpg.connect(f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}/postgres")
    try:
        if not await admin.fetchval("select 1 from pg_database where datname = $1", TEST_DB):
            await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()


@pytest.fixture(scope="session", autouse=True)
async def database():
    await _create_database_if_missing()
    async with common_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await common_db.engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(database):
    """Every test starts from an empty jobs/audit_log table."""
    async with common_db.engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE jobs, audit_log CASCADE")
    yield
