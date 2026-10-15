"""Hybrid knowledge-base search (ADR 0004, SDD "База знань і RAG", step 4).

Two candidate lists come from PostgreSQL: the nearest chunks by cosine distance (HNSW)
and the best ``simple`` full-text matches with prefix terms. Reciprocal Rank Fusion merges
them by rank alone, so the two incomparable scores never need calibrating. The "nothing
found" decision uses the best vector similarity, because an RRF score measures position,
not relevance: if no chunk is semantically close, the result is empty even when a word
matched.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rozmova_core.models import KbChunk, KbDocument
from rozmova_engine.rag.embeddings import Embedder

DEFAULT_TOP_K = 5
DEFAULT_MIN_SIMILARITY = 0.35
DEFAULT_CANDIDATES = 20
# The constant from the RRF paper (Cormack et al., 2009); it damps the head of each list.
RRF_K = 60

# Question words and fillers that carry no topic. With prefix matching, ``як:*`` would
# hit half of the knowledge base. Words shorter than MIN_TERM_LENGTH are dropped anyway.
_STOP_WORDS_TEXT = """
але або аби адже будь буде будуть був була були було вам вас ваш ваша ваше ваші весь
все всі вже від для добрий день його йому коли куди ласка мене мені мій мною моє мої
моя можна може можу мого моєї моєму мою нас наш наша наше наші нема немає них ось
підкажіть привіт про розкажіть саме своє свій свої своя скажіть скільки собі так
також там тебе тепер тобі тому треба тут хочу хочемо цей цього цю ця цієї чому чого
через щось який яка яке які якщо якій якого яку
"""
STOP_WORDS = frozenset(_STOP_WORDS_TEXT.split())
# Word starts that occur in a third of the chunks (how to enable a service, what it
# costs): as a lone OR term they pull in every "how to enable" section. The vector
# branch still carries their meaning.
DOMAIN_STOP_STEMS = ("підключ", "відключ", "кошту", "коштує", "вартіст")
MIN_TERM_LENGTH = 3
# Words this long lose their last two letters (keeping at least four) before the prefix
# match: a crude stand-in for Ukrainian stemming, so «роумінгу» and «тарифи» still find
# «роумінг» and «тариф». Shorter words such as «тариф» are matched as typed.
STEM_FROM_LENGTH = 6
STEM_CUT = 2
STEM_MIN_LENGTH = 4

_WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One retrieved chunk. ``similarity`` is the cosine similarity to the query; a rank
    is ``None`` when the chunk was not among that branch's candidates."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_path: str
    section: str
    text: str
    score: float
    similarity: float
    vector_rank: int | None
    text_rank: int | None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """``hits`` is empty when ``best_similarity`` is below the threshold."""

    query: str
    hits: list[SearchHit]
    best_similarity: float | None

    @property
    def found(self) -> bool:
        return bool(self.hits)


def text_query(query: str) -> str | None:
    """A ``to_tsquery('simple', …)`` string: topic words as OR-ed prefix terms.

    Terms contain letters and digits only, so the string needs no escaping. ``None`` when
    nothing but stop words is left.
    """
    terms: list[str] = []
    for word in _WORD.findall(query.lower()):
        if len(word) < MIN_TERM_LENGTH or word in STOP_WORDS or word.startswith(DOMAIN_STOP_STEMS):
            continue
        if len(word) >= STEM_FROM_LENGTH and not word.isdigit():
            word = word[: max(STEM_MIN_LENGTH, len(word) - STEM_CUT)]
        if word not in terms:
            terms.append(word)
    return " | ".join(f"{term}:*" for term in terms) or None


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[uuid.UUID]], k: int = RRF_K
) -> list[tuple[uuid.UUID, float]]:
    """Ids by descending ``sum(1 / (k + rank))`` over the lists (rank is 1-based).

    Ties keep the order of first appearance, so the vector branch, listed first, wins them.
    """
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


async def search_kb(
    db: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    candidates: int = DEFAULT_CANDIDATES,
) -> SearchResult:
    """Top ``top_k`` chunks for ``query``, or none if nothing is semantically close."""
    query = query.strip()
    if not query:
        return SearchResult(query=query, hits=[], best_similarity=None)
    [vector] = await embedder.embed([query])
    distance = KbChunk.embedding.cosine_distance(vector)

    vector_rows = (
        await db.execute(select(KbChunk.id, distance).order_by(distance).limit(candidates))
    ).all()
    if not vector_rows:
        return SearchResult(query=query, hits=[], best_similarity=None)
    best_similarity = 1.0 - float(vector_rows[0][1])
    if best_similarity < min_similarity:
        return SearchResult(query=query, hits=[], best_similarity=best_similarity)

    text_ids: list[uuid.UUID] = []
    terms = text_query(query)
    if terms is not None:
        tsquery = func.to_tsquery("simple", terms)
        rank = func.ts_rank_cd(KbChunk.tsv, tsquery)
        text_ids = list(
            await db.scalars(
                select(KbChunk.id)
                .where(KbChunk.tsv.op("@@")(tsquery))
                .order_by(rank.desc(), KbChunk.id)
                .limit(candidates)
            )
        )

    vector_ids = [row[0] for row in vector_rows]
    fused = reciprocal_rank_fusion([vector_ids, text_ids])[:top_k]
    rows = {
        row.id: row
        for row in await db.execute(
            select(
                KbChunk.id,
                KbChunk.document_id,
                KbChunk.section,
                KbChunk.text,
                KbDocument.title,
                KbDocument.source_path,
                (1.0 - distance).label("similarity"),
            )
            .join(KbDocument)
            .where(KbChunk.id.in_([chunk_id for chunk_id, _ in fused]))
        )
    }
    vector_rank = {chunk_id: n for n, chunk_id in enumerate(vector_ids, start=1)}
    text_rank = {chunk_id: n for n, chunk_id in enumerate(text_ids, start=1)}
    hits = [
        SearchHit(
            chunk_id=chunk_id,
            document_id=rows[chunk_id].document_id,
            document_title=rows[chunk_id].title,
            source_path=rows[chunk_id].source_path,
            section=rows[chunk_id].section,
            text=rows[chunk_id].text,
            score=score,
            similarity=float(rows[chunk_id].similarity),
            vector_rank=vector_rank.get(chunk_id),
            text_rank=text_rank.get(chunk_id),
        )
        # A chunk deleted by a concurrent ingest between the queries is simply left out.
        for chunk_id, score in fused
        if chunk_id in rows
    ]
    return SearchResult(query=query, hits=hits, best_similarity=best_similarity)
