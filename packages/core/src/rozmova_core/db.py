"""Async SQLAlchemy engine and session factory (asyncpg)."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rozmova_core.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide engine; dispose it with ``await engine.dispose()`` on shutdown."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions keep loaded attributes after commit, so results stay usable outside the session."""
    return async_sessionmaker(engine, expire_on_commit=False)
