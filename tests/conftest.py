"""Shared fixtures: the real API on a per-test SQLite file, and the bot's API client wired to it in-process."""

from typing import TYPE_CHECKING

import pytest
from advanced_alchemy.base import UUIDAuditBase
from litestar.testing import AsyncTestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import sidequest_common.models  # noqa: F401  # register tables on UUIDAuditBase.metadata
from sidequest_api.app import create_app
from sidequest_bot.api_client import API_KEY_HEADER, SideQuestAPI
from sidequest_common.settings import APISettings, DatabaseSettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from litestar import Litestar

TEST_API_KEY = "test-api-key"
GUILD_ID = 1564671275316543508


@pytest.fixture
async def database_url(tmp_path: Path) -> str:
    """SQLite database file with the full schema, fresh per test."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(UUIDAuditBase.metadata.create_all)
    await engine.dispose()
    return url


@pytest.fixture
async def db_sessionmaker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory on the test database, for exercising services directly (e.g. concurrent-insert races)."""
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def api_app(database_url: str) -> Litestar:
    """Fresh app on the per-test database."""
    return create_app(DatabaseSettings(URL=database_url), APISettings(KEY=TEST_API_KEY))


@pytest.fixture
async def client(api_app: Litestar) -> AsyncIterator[AsyncTestClient[Litestar]]:
    """HTTP client for the API, already authenticated."""
    async with AsyncTestClient(app=api_app) as test_client:
        test_client.headers[API_KEY_HEADER] = TEST_API_KEY
        yield test_client


@pytest.fixture
async def api(client: AsyncTestClient[Litestar]) -> SideQuestAPI:
    """The bot's API client, talking to the in-process API."""
    return SideQuestAPI(str(client.base_url), TEST_API_KEY, client=client)
