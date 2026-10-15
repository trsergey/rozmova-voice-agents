"""Hybrid search: query parsing and RRF (unit), ranking on the ingested KB (integration)."""

import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from rozmova_core.db import create_session_factory
from rozmova_core.models import KbChunk
from rozmova_engine.rag.embeddings import FakeEmbedder, fake_vector
from rozmova_engine.rag.ingest import ingest_directory
from rozmova_engine.rag.search import reciprocal_rank_fusion, search_kb, text_query

KB_DIR = Path(__file__).resolve().parents[3] / "kb"


# --- query parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Безлім", "безл:*"),
        # Long words lose two letters: «роумінгу» finds «роумінг», «Європі» finds «Європа».
        ("Роумінгу в Європі", "роумін:* | євро:*"),
        # «тариф» is short enough to match as typed; duplicates collapse.
        ("тариф Безлім, тариф!", "тариф:* | безл:*"),
        ("eSIM на 5G", "esim:*"),
        ("12345 абонплата", "12345:* | абонпла:*"),
        # Question words, fillers and "how to enable / what it costs" carry no topic.
        ("Скажіть, будь ласка, скільки коштує і як підключити?", None),
        ("", None),
    ],
)
def test_text_query(query: str, expected: str | None) -> None:
    assert text_query(query) == expected


def test_text_query_output_is_safe_for_to_tsquery() -> None:
    hostile = "безлім') | !(x & 'y':* <-> \\ ; DROP TABLE"

    terms = text_query(hostile)

    assert terms is not None
    assert all(term.endswith(":*") and term[:-2].isalnum() for term in terms.split(" | "))


# --- reciprocal rank fusion --------------------------------------------------------

A, B, C, D = (uuid.UUID(int=n) for n in range(1, 5))


def test_rrf_prefers_items_ranked_in_both_lists() -> None:
    fused = reciprocal_rank_fusion([[A, B, C], [C, D, B]], k=60)

    assert [item for item, _ in fused] == [C, B, A, D]
    assert fused[0][1] == pytest.approx(1 / 63 + 1 / 61)


def test_rrf_ties_keep_first_appearance() -> None:
    fused = reciprocal_rank_fusion([[A, B], [B, A]])

    assert [item for item, _ in fused] == [A, B]


def test_rrf_with_an_empty_list_is_the_other_list() -> None:
    assert [item for item, _ in reciprocal_rank_fusion([[A, B], []])] == [A, B]


# --- search on the ingested knowledge base ------------------------------------------


class QueryEmbedder:
    """Embeds every query as a fixed vector, to steer the vector branch."""

    model = "fake-embedding"

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [self.vector for _ in texts]


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with create_session_factory(engine)() as session:
        yield session


@pytest.fixture
async def kb(engine: AsyncEngine) -> None:
    await ingest_directory(KB_DIR, engine, FakeEmbedder())


async def _chunk(db: AsyncSession, section_prefix: str) -> KbChunk:
    chunk = await db.scalar(select(KbChunk).where(KbChunk.section.startswith(section_prefix)))
    assert chunk is not None, section_prefix
    return chunk


@pytest.mark.integration
@pytest.mark.usefixtures("kb")
async def test_chunk_close_in_meaning_and_wording_comes_first(db: AsyncSession) -> None:
    target = await _chunk(db, "Вартість тарифу «Безлім»")

    result = await search_kb(db, QueryEmbedder(fake_vector(target.text)), "Безлім")

    assert result.found
    assert len(result.hits) == 5
    top = result.hits[0]
    assert top.chunk_id == target.id
    assert top.similarity == pytest.approx(1.0) == result.best_similarity
    assert top.vector_rank == 1
    assert top.text_rank is not None
    assert top.source_path == "tariffs/bezlim.md"
    assert top.document_title == "Тариф «Безлім»"
    assert top.section.startswith("Вартість тарифу «Безлім»")
    assert [hit.score for hit in result.hits] == sorted(
        (hit.score for hit in result.hits), reverse=True
    )


@pytest.mark.integration
@pytest.mark.usefixtures("kb")
async def test_chunk_found_by_both_branches_outranks_a_vector_only_one(
    db: AsyncSession,
) -> None:
    antispam = await _chunk(db, "Що входить у послугу «Антиспам»")
    roaming = await _chunk(db, "Що входить у послугу «Роумінг Європа»")
    # Equally close to both chunks in meaning; only one of them contains the word.
    pairs = zip(fake_vector(antispam.text), fake_vector(roaming.text), strict=True)
    mixed = [a + b for a, b in pairs]
    norm = sum(v * v for v in mixed) ** 0.5

    result = await search_kb(db, QueryEmbedder([v / norm for v in mixed]), "Антиспам")

    by_id = {hit.chunk_id: hit for hit in result.hits}
    assert result.hits[0].chunk_id == antispam.id
    assert by_id[antispam.id].text_rank is not None
    assert by_id[roaming.id].text_rank is None
    assert by_id[roaming.id].similarity == pytest.approx(by_id[antispam.id].similarity, abs=0.05)
    assert by_id[antispam.id].score > by_id[roaming.id].score


@pytest.mark.integration
@pytest.mark.usefixtures("kb")
async def test_nothing_close_enough_gives_an_empty_result(db: AsyncSession) -> None:
    # A random direction: cosine similarity to every chunk is about zero, although the
    # words of the query match the «Безлім» document exactly.
    result = await search_kb(db, QueryEmbedder(fake_vector("Космос")), "тариф Безлім")

    assert not result.found
    assert result.best_similarity is not None
    assert result.best_similarity < 0.35


@pytest.mark.integration
@pytest.mark.usefixtures("kb")
async def test_threshold_is_configurable(db: AsyncSession) -> None:
    result = await search_kb(
        db, QueryEmbedder(fake_vector("Космос")), "тариф Безлім", min_similarity=-1.0, top_k=3
    )

    assert len(result.hits) == 3
    # With no semantic signal, the chunks that matched the words lead.
    assert all(hit.text_rank is not None for hit in result.hits)


@pytest.mark.integration
@pytest.mark.usefixtures("kb")
async def test_stop_word_query_uses_the_vector_branch_only(db: AsyncSession) -> None:
    target = await _chunk(db, "Як отримати eSIM")

    result = await search_kb(db, QueryEmbedder(fake_vector(target.text)), "скажіть, як це?")

    assert result.hits[0].chunk_id == target.id
    assert all(hit.text_rank is None for hit in result.hits)


@pytest.mark.integration
async def test_empty_knowledge_base_finds_nothing(db: AsyncSession) -> None:
    result = await search_kb(db, QueryEmbedder(fake_vector("x")), "Безлім")

    assert not result.found
    assert result.best_similarity is None


async def test_blank_query_does_not_call_the_embedder() -> None:
    embedder = QueryEmbedder(fake_vector("x"))

    result = await search_kb(None, embedder, "   ")  # type: ignore[arg-type]

    assert not result.found
    assert embedder.calls == 0
