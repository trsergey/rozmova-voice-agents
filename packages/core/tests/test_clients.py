from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings


async def test_engine_and_session_factory_without_connecting() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db:5432/rozmova"), db_pool_size=3
    )

    engine = create_engine(settings)
    factory = create_session_factory(engine)

    assert isinstance(engine, AsyncEngine)
    assert engine.dialect.driver == "asyncpg"
    assert engine.pool.size() == 3  # type: ignore[attr-defined]
    assert "p@" not in repr(engine.url)
    async with factory() as session:
        assert isinstance(session, AsyncSession)
    await engine.dispose()


async def test_redis_client_without_connecting() -> None:
    client = create_redis(Settings(redis_url=SecretStr("redis://cache:6380/2")))

    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("cache", 6380, 2)
    assert kwargs["decode_responses"] is True
    await client.aclose()


@pytest.fixture(scope="module")
def containers() -> Iterator[Settings]:
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    with (
        PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg,
        RedisContainer("redis:7-alpine") as redis,
    ):
        redis_url = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
        yield Settings(
            database_url=SecretStr(pg.get_connection_url()), redis_url=SecretStr(redis_url)
        )


@pytest.mark.integration
async def test_real_postgres_and_redis(containers: Settings) -> None:
    engine = create_engine(containers)
    try:
        async with create_session_factory(engine)() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()

    client = create_redis(containers)
    try:
        assert await client.ping()
    finally:
        await client.aclose()
