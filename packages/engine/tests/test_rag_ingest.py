"""Incremental KB ingest against a migrated PostgreSQL with pgvector (testcontainers)."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Connection, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import KbChunk, KbDocument
from rozmova_core.settings import Settings
from rozmova_engine.rag.embeddings import FakeEmbedder
from rozmova_engine.rag.ingest import (
    INGEST_LOCK_KEY,
    build_embedder,
    ingest_directory,
    kb_files,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
KB_DIR = ROOT / "kb"
ALEMBIC_INI = ROOT / "packages/core/alembic.ini"


@pytest.fixture(scope="module")
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


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    shutil.copytree(KB_DIR, root)
    return root


async def _chunks(engine: AsyncEngine, source_path: str) -> list[KbChunk]:
    async with create_session_factory(engine)() as db:
        rows = await db.scalars(
            select(KbChunk)
            .join(KbDocument)
            .where(KbDocument.source_path == source_path)
            .order_by(KbChunk.position)
        )
        return list(rows)


async def test_first_ingest_indexes_every_document(engine: AsyncEngine, kb: Path) -> None:
    embedder = FakeEmbedder()

    report = await ingest_directory(kb, engine, embedder)

    assert report.documents_added == len(kb_files(kb)) == 15
    assert report.chunks_embedded == len(embedder.embedded_texts) > report.documents_added
    # One embedding request per document, never one per chunk.
    assert len(embedder.calls) == report.documents_added
    async with engine.connect() as conn:
        titles = set(await conn.scalars(select(KbDocument.title)))
        assert {"Тариф «Безлім»", "Послуга «Роумінг Європа»"} <= titles
        assert "Knowledge base" not in titles  # kb/README.md is not ingested
        # tsv comes from the `simple` config: lower-cased words, no stemming.
        hits = await conn.scalar(
            select(func.count())
            .select_from(KbChunk)
            .where(KbChunk.tsv.op("@@")(func.to_tsquery("simple", "безлім")))
        )
        assert hits is not None
        assert hits >= 1
        # The HNSW index answers a cosine query.
        probe = (await _chunks(engine, "tariffs/bezlim.md"))[0]
        nearest = await conn.scalar(
            select(KbChunk.id).order_by(KbChunk.embedding.cosine_distance(probe.embedding)).limit(1)
        )
        assert nearest == probe.id


async def test_second_ingest_skips_unchanged_documents(engine: AsyncEngine, kb: Path) -> None:
    await ingest_directory(kb, engine, FakeEmbedder())
    embedder = FakeEmbedder()

    report = await ingest_directory(kb, engine, embedder)

    assert report.documents_unchanged == 15
    assert report.documents_added == report.documents_updated == report.chunks_embedded == 0
    assert embedder.calls == []


async def test_edit_re_embeds_only_the_changed_chunk(engine: AsyncEngine, kb: Path) -> None:
    await ingest_directory(kb, engine, FakeEmbedder())
    before = await _chunks(engine, "tariffs/bezlim.md")
    path = kb / "tariffs" / "bezlim.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("395 грн на місяць", "405 грн на місяць"),
        encoding="utf-8",
    )
    embedder = FakeEmbedder()

    report = await ingest_directory(kb, engine, embedder)

    after = await _chunks(engine, "tariffs/bezlim.md")
    assert report.documents_updated == 1
    assert report.documents_unchanged == 14
    assert report.chunks_embedded == report.chunks_deleted == 1
    assert report.chunks_reused == len(before) - 1
    assert len(embedder.embedded_texts) == 1
    assert "405 грн" in embedder.embedded_texts[0]
    # Unchanged chunks keep their ids (citations in call logs stay valid).
    changed = [
        i for i, (old, new) in enumerate(zip(before, after, strict=True)) if old.id != new.id
    ]
    assert len(changed) == 1
    assert "405 грн" in after[changed[0]].text


async def test_new_section_shifts_positions_and_keeps_ids(engine: AsyncEngine, kb: Path) -> None:
    await ingest_directory(kb, engine, FakeEmbedder())
    before = await _chunks(engine, "tariffs/start.md")
    path = kb / "tariffs" / "start.md"
    first_section = path.read_text(encoding="utf-8").index("\n## ")
    original = path.read_text(encoding="utf-8")
    long_section = "\n\n## Акція для тарифу «Старт»\n\n" + " ".join(
        f"Акційна умова {i} тарифу «Старт» діє до кінця року." for i in range(40)
    )
    path.write_text(original[:first_section] + long_section + original[first_section:])

    report = await ingest_directory(kb, engine, FakeEmbedder())

    after = await _chunks(engine, "tariffs/start.md")
    assert [chunk.position for chunk in after] == list(range(len(after)))
    assert after[0].section == "Акція для тарифу «Старт»"
    assert {chunk.id for chunk in before} <= {chunk.id for chunk in after}
    assert report.chunks_reused == len(before)


async def test_removed_file_deletes_its_document_and_chunks(engine: AsyncEngine, kb: Path) -> None:
    await ingest_directory(kb, engine, FakeEmbedder())
    (kb / "services" / "music.md").unlink()

    report = await ingest_directory(kb, engine, FakeEmbedder())

    assert report.documents_deleted == 1
    assert await _chunks(engine, "services/music.md") == []
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(KbDocument)) == 14


async def test_changed_embedding_model_re_embeds_everything(engine: AsyncEngine, kb: Path) -> None:
    first = await ingest_directory(kb, engine, FakeEmbedder())

    report = await ingest_directory(kb, engine, FakeEmbedder(model="another-model"))

    assert report.documents_updated == 15
    assert report.chunks_embedded == first.chunks_embedded
    assert report.chunks_reused == 0


async def test_ingest_is_skipped_while_another_holds_the_lock(
    engine: AsyncEngine, kb: Path
) -> None:
    embedder = FakeEmbedder()
    async with engine.connect() as other:
        assert await other.scalar(select(func.pg_try_advisory_lock(INGEST_LOCK_KEY)))
        try:
            report = await ingest_directory(kb, engine, embedder)
        finally:
            await other.execute(select(func.pg_advisory_unlock(INGEST_LOCK_KEY)))

    assert report.skipped_locked
    assert embedder.calls == []
    # The lock is released after a normal run, so the next one proceeds.
    assert (await ingest_directory(kb, engine, embedder)).documents_added == 15


async def test_missing_directory_fails(engine: AsyncEngine, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ingest_directory(tmp_path / "absent", engine, FakeEmbedder())


def test_build_embedder_needs_a_key_unless_fake() -> None:
    settings = Settings(openai_api_key=None)

    assert isinstance(build_embedder(settings, fake=True), FakeEmbedder)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_embedder(settings)
