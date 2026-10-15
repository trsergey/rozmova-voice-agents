"""A migrated PostgreSQL with pgvector for the knowledge-base integration tests."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rozmova_core.db import create_engine
from rozmova_core.settings import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/core/alembic.ini"


@pytest.fixture(scope="session")
def database_url() -> Iterator[SecretStr]:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        url = SecretStr(pg.get_connection_url())

        async def migrate() -> None:
            engine = create_engine(Settings(database_url=url))

            def upgrade(connection: Connection) -> None:
                config = Config(str(ALEMBIC_INI))
                config.attributes["configure_logging"] = False
                config.attributes["connection"] = connection
                command.upgrade(config, "head")

            async with engine.begin() as conn:
                await conn.run_sync(upgrade)
            await engine.dispose()

        asyncio.run(migrate())
        yield url


@pytest.fixture
async def engine(database_url: SecretStr) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings(database_url=database_url))
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE kb_documents CASCADE"))
    try:
        yield engine
    finally:
        await engine.dispose()
