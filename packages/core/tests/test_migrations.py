"""Alembic migrations: offline SQL without Docker, upgrade/downgrade on a real pgvector."""

import io
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rozmova_core.db import create_engine
from rozmova_core.models import Base
from rozmova_core.settings import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

SDD_TABLES = {
    "bots",
    "bot_versions",
    "provider_credentials",
    "users",
    "access_codes",
    "calls",
    "call_events",
    "kb_documents",
    "kb_chunks",
}


def _config(connection: Connection | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logging"] = False
    if connection is not None:
        config.attributes["connection"] = connection
    return config


# --- No Docker ---------------------------------------------------------------


def test_revisions_form_one_linear_chain_with_downgrades() -> None:
    script = ScriptDirectory.from_config(_config())

    assert len(script.get_heads()) == 1
    assert len(script.get_bases()) == 1
    for revision in script.walk_revisions():
        source = Path(revision.path).read_text(encoding="utf-8")
        downgrade = source.split("def downgrade() -> None:", 1)[1]
        assert downgrade.strip() != "pass", f"{revision.revision} has an empty downgrade"


def test_offline_sql_has_extension_and_indexes() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        command.upgrade(_config(), "head", sql=True)
    sql = out.getvalue()

    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    for table in SDD_TABLES:
        assert f"CREATE TABLE {table} " in sql
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "USING gin (tsv)" in sql
    assert "USING gin (flow jsonb_path_ops)" in sql
    assert "USING gin (draft_flow jsonb_path_ops)" in sql


# --- Docker (testcontainers) ---------------------------------------------------


@pytest.fixture(scope="module")
def pg_settings() -> Iterator[Settings]:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield Settings(database_url=SecretStr(pg.get_connection_url()))


@pytest.fixture
async def engine(pg_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(pg_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _alembic(engine: AsyncEngine, action: str, target: str) -> None:
    def run(connection: Connection) -> None:
        getattr(command, action)(_config(connection), target)

    async with engine.begin() as conn:
        await conn.run_sync(run)


async def _scalars(conn: AsyncConnection, sql: str) -> set[str]:
    return set((await conn.execute(text(sql))).scalars())


async def _index_defs(conn: AsyncConnection) -> dict[str, str]:
    rows = await conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
    )
    return dict(rows.all())


PUBLIC_TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
EXTENSIONS = "SELECT extname FROM pg_extension"


@pytest.mark.integration
async def test_upgrade_head_creates_schema_matching_models(engine: AsyncEngine) -> None:
    await _alembic(engine, "upgrade", "head")
    try:
        async with engine.connect() as conn:
            assert await _scalars(conn, PUBLIC_TABLES) == SDD_TABLES | {"alembic_version"}
            assert "vector" in await _scalars(conn, EXTENSIONS)

            indexes = await _index_defs(conn)
            assert "USING hnsw (embedding vector_cosine_ops)" in indexes["ix_kb_chunks_embedding"]
            assert "USING gin (tsv)" in indexes["ix_kb_chunks_tsv"]
            assert "USING gin (flow jsonb_path_ops)" in indexes["ix_bot_versions_flow"]
            assert "USING gin (draft_flow jsonb_path_ops)" in indexes["ix_bots_draft_flow"]

            def diff(sync_conn: Connection) -> list[object]:
                context = MigrationContext.configure(sync_conn)
                return list(compare_metadata(context, Base.metadata))

            assert await conn.run_sync(diff) == []
    finally:
        await _alembic(engine, "downgrade", "base")


@pytest.mark.integration
async def test_schema_supports_sdd_queries(engine: AsyncEngine) -> None:
    await _alembic(engine, "upgrade", "head")
    try:
        async with engine.begin() as conn:
            bot_id = (
                await conn.execute(
                    text(
                        "INSERT INTO bots (name, provider, model) "
                        "VALUES ('Хвиля', 'openai', 'gpt-4.1-mini') RETURNING id"
                    )
                )
            ).scalar_one()
            flow = {"nodes": [{"id": "assist", "data": {"tools": ["activate_service"]}}]}
            version_id = (
                await conn.execute(
                    text(
                        "INSERT INTO bot_versions (bot_id, version, flow) "
                        "VALUES (:bot, 1, CAST(:flow AS jsonb)) RETURNING id"
                    ),
                    {"bot": bot_id, "flow": json.dumps(flow)},
                )
            ).scalar_one()
            # The bots <-> bot_versions cycle: the live pointer is settable after publish.
            await conn.execute(
                text("UPDATE bots SET live_version_id = :v WHERE id = :b"),
                {"v": version_id, "b": bot_id},
            )

            # FR-18: find versions that use a tool (SDD query).
            usages = await conn.execute(
                text(
                    "SELECT b.name, v.version FROM bot_versions v JOIN bots b ON b.id = v.bot_id "
                    """WHERE v.flow @> '{"nodes": [{"data": {"tools": ["activate_service"]}}]}'"""
                )
            )
            assert usages.all() == [("Хвиля", 1)]

            # call_events: the Redis Stream id makes re-delivery idempotent.
            call_id = (
                await conn.execute(
                    text("INSERT INTO calls (bot_id) VALUES (:b) RETURNING id"), {"b": bot_id}
                )
            ).scalar_one()
            insert_event = text(
                "INSERT INTO call_events (id, call_id, ts, type) "
                "VALUES ('1697040000000-0', :c, now(), 'turn_end') ON CONFLICT (id) DO NOTHING"
            )
            await conn.execute(insert_event, {"c": call_id})
            await conn.execute(insert_event, {"c": call_id})
            count = await conn.execute(text("SELECT count(*) FROM call_events"))
            assert count.scalar_one() == 1

            # kb_chunks: tsv is generated with the `simple` config; cosine search works.
            doc_id = (
                await conn.execute(
                    text(
                        "INSERT INTO kb_documents (title, source_path, content_hash) "
                        "VALUES ('Роумінг', 'kb/roaming.md', 'h') RETURNING id"
                    )
                )
            ).scalar_one()
            embedding = "[" + ",".join(["0.1"] * 1536) + "]"
            await conn.execute(
                text(
                    "INSERT INTO kb_chunks "
                    "(document_id, position, section, text, content_hash, embedding) "
                    "VALUES (:d, 0, 'Роумінг Європа', 'Послуга коштує 99 грн', 'h', :e)"
                ),
                {"d": doc_id, "e": embedding},
            )
            hit = await conn.execute(
                text(
                    "SELECT section, 1 - (embedding <=> CAST(:e AS vector)) FROM kb_chunks "
                    "WHERE tsv @@ to_tsquery('simple', 'роумінг:*')"
                ),
                {"e": embedding},
            )
            section, similarity = hit.one()
            assert section == "Роумінг Європа"
            assert similarity == pytest.approx(1.0)

            await conn.rollback()
    finally:
        await _alembic(engine, "downgrade", "base")


@pytest.mark.integration
async def test_constraints_reject_invalid_rows(engine: AsyncEngine) -> None:
    await _alembic(engine, "upgrade", "head")
    try:
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO users (email, password_hash, role) VALUES ('a@b', 'x', 'root')"
                    )
                )
            await conn.rollback()
    finally:
        await _alembic(engine, "downgrade", "base")


@pytest.mark.integration
async def test_downgrade_base_leaves_clean_database(engine: AsyncEngine) -> None:
    await _alembic(engine, "upgrade", "head")
    await _alembic(engine, "downgrade", "base")

    async with engine.connect() as conn:
        assert await _scalars(conn, PUBLIC_TABLES) == {"alembic_version"}
        assert "vector" not in await _scalars(conn, EXTENSIONS)

    # And the cycle repeats on the same database.
    await _alembic(engine, "upgrade", "head")
    await _alembic(engine, "downgrade", "base")
