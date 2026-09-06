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

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

TEST_DB = os.environ.get("TEST_DB_NAME", "image23d_test")
# Services are published on 127.0.0.1 (see docker-compose.yml), while .env uses
# the compose network's hostnames -- so the host has to be rewritten either way.
TEST_HOST = os.environ.get("TEST_HOST", "localhost")


def _dotenv(name: str, default: str) -> str:
    """Read one key out of .env.

    Credentials are derived from the real configuration rather than hardcoded,
    so setting a Redis password or rotating the Postgres one does not silently
    leave the test suite pointing at values that no longer exist.
    """
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and not line.startswith("#"):
                return line.split("=", 1)[1]
    return default


def _retarget(url: str, *, host: str, path: str) -> str:
    """Point a service URL at the host-published port and an isolated database,
    keeping whatever credentials it already carries."""
    parts = urlsplit(url)
    userinfo = ""
    if parts.username is not None or parts.password is not None:
        userinfo = f"{parts.username or ''}:{parts.password or ''}@"
    netloc = f"{userinfo}{host}:{parts.port}" if parts.port else f"{userinfo}{host}"
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


_DATABASE_URL = _dotenv("DATABASE_URL", "postgresql+asyncpg://image23d:image23d@postgres:5432/image23d")
_REDIS_URL = _dotenv("REDIS_URL", "redis://redis:6379/0")

os.environ["DATABASE_URL"] = _retarget(_DATABASE_URL, host=TEST_HOST, path=f"/{TEST_DB}")
# db 15, well away from the app's db 0.
os.environ["REDIS_URL"] = os.environ.get(
    "TEST_REDIS_URL", _retarget(_REDIS_URL, host=TEST_HOST, path="/15")
)
# Admin connection for CREATE DATABASE -- same credentials, the `postgres` db,
# and a plain driver since asyncpg is used directly rather than through SQLAlchemy.
_ADMIN_URL = _retarget(_DATABASE_URL, host=TEST_HOST, path="/postgres").replace("+asyncpg", "")

import asyncio  # noqa: E402

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
    admin = await asyncpg.connect(_ADMIN_URL)
    try:
        if not await admin.fetchval("select 1 from pg_database where datname = $1", TEST_DB):
            await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()


def _upgrade_to_head() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    # alembic/env.py reads common.settings.database_url, which conftest has
    # already pointed at the test database.
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def database():
    """Rebuild the test schema from the migrations, not from create_all().

    create_all() only creates tables that don't exist -- it silently ignores
    added columns on existing ones, which is exactly how Phase 4's Job.created_by
    went missing. Running the real migration chain here means a model change
    without a matching migration fails the suite instead of passing it.
    """
    await _create_database_if_missing()
    async with common_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    # alembic's env.py calls asyncio.run() itself, so it cannot run on this loop.
    await asyncio.to_thread(_upgrade_to_head)
    yield
    await common_db.engine.dispose()


@pytest.fixture
async def clean_tables(database):
    """Every test that asks for it starts from empty jobs/audit_log tables.

    Opt-in rather than autouse: most of this suite is pure logic (stage
    attribution, bbox cropping, parameter validation) and requiring a live
    Postgres for those makes them unrunnable on a laptop and slow everywhere.
    Modules that do need the database declare it with

        pytestmark = pytest.mark.usefixtures("clean_tables")
    """
    async with common_db.engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE jobs, audit_log CASCADE")
    yield
