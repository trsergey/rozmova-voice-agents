"""Answers from the knowledge base: the ``kb_answer`` node and the ``search_knowledge_base``
tool (SDD "База знань і RAG", step 5; FR-3).

The retrieved chunks go to the LLM numbered ``[1]…[n]`` in a system message that forbids
facts outside them and asks for ``[n]`` after each statement. The reply streams as is;
the markers it contains become ``turn_end.sources``. With no chunk above the node's
threshold the node says its ``not_found_reply`` and calls no LLM.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rozmova_contracts import protocol
from rozmova_contracts.flow import KBAnswerNode
from rozmova_engine.flow.executor import KBReply
from rozmova_engine.providers import Message
from rozmova_engine.rag.embeddings import Embedder
from rozmova_engine.rag.search import SearchHit, SearchResult, search_kb
from rozmova_engine.session import HistoryEntry, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

Emit = Callable[[protocol.ProtocolMessage], Awaitable[None]]

# Embedding the query and two SQL queries; well inside the 5 s tool budget.
SEARCH_TIMEOUT_S = 4.0
SEARCH_TOOL = "search_knowledge_base"

KB_SYSTEM_PROMPT = (
    "Ти асистент підтримки Hvylia Mobile. Відповідай українською, коротко й по суті, "
    "лише на основі пронумерованих фрагментів бази знань нижче. Не додавай цін, умов, "
    "назв чи інших фактів, яких немає у фрагментах, навіть якщо вони здаються очевидними. "
    "Після кожного твердження став номер фрагмента, з якого воно взяте, у квадратних "
    "дужках: [1] або [2][3]. Якщо абонент питає про тариф, послугу чи умову, якої у "
    "фрагментах немає, прямо скажи, що в базі знань такої інформації немає, і запропонуй "
    "перевести на оператора; не підставляй замість неї схожий тариф чи послугу."
)
FRAGMENTS_HEADER = "Фрагменти бази знань:"

_MARKER = re.compile(r"\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\]")


class KnowledgeSearch(Protocol):
    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        """Raises on a database or embedding failure, including a timeout."""
        ...


class KnowledgeBase:
    """Hybrid search over ``kb_chunks`` with a session per query and a bounded wait."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        timeout_s: float = SEARCH_TIMEOUT_S,
    ) -> None:
        self._sessions = sessions
        self._embedder = embedder
        self._timeout_s = timeout_s

    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        async with asyncio.timeout(self._timeout_s), self._sessions() as db:
            return await search_kb(
                db, self._embedder, query, top_k=top_k, min_similarity=min_similarity
            )

    async def aclose(self) -> None:
        """Close the embedder's HTTP client; the database engine belongs to the caller."""
        close = getattr(self._embedder, "aclose", None)
        if close is not None:
            await close()


class UnavailableKnowledgeBase:
    """Stands in when no embedding key is configured: every search fails, so a
    ``kb_answer`` node reports ``kb_unavailable`` and says its not-found reply."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        raise RuntimeError(f"the knowledge base is not configured: {self.reason}")


def fragments_prompt(hits: Sequence[SearchHit]) -> str:
    """The numbered chunks; each chunk already starts with its document title."""
    blocks = [f"[{n}] {hit.text}" for n, hit in enumerate(hits, start=1)]
    return "\n\n".join([FRAGMENTS_HEADER, *blocks])


def cited_sources(text: str, hits: Sequence[SearchHit]) -> list[protocol.Source]:
    """Sources for the ``[n]`` markers in ``text``, in order of first citation.

    Markers outside ``1..len(hits)`` are ignored; ``[1, 2]`` counts as two markers.
    """
    sources: list[protocol.Source] = []
    seen: set[int] = set()
    for match in _MARKER.finditer(text):
        for number in (int(part) for part in match.group(1).split(",")):
            if number in seen or not 1 <= number <= len(hits):
                continue
            seen.add(number)
            hit = hits[number - 1]
            sources.append(
                protocol.Source(
                    marker=number,
                    chunk_id=str(hit.chunk_id),
                    document_id=str(hit.document_id),
                    document_title=hit.document_title,
                    section=hit.section,
                )
            )
    return sources


class KnowledgeBaseAnswerer:
    """``KBAnswerer`` for one session: searches, then answers through the session's
    ``ToolLoop`` (no tools), so streaming, the first-token timeout and barge-in behave as
    in every other LLM step. The reply is recorded in history here."""

    def __init__(self, kb: KnowledgeSearch, loop: ToolLoop, emit: Emit) -> None:
        self._kb = kb
        self._loop = loop
        self._emit = emit

    async def answer(self, node: KBAnswerNode, state: SessionState, timer: TurnTimer) -> KBReply:
        query = _last_user_text(state)
        try:
            with timer.stage("rag_search"):
                result = await self._kb.search(
                    query, top_k=node.data.top_k, min_similarity=node.data.min_similarity
                )
        except Exception:
            log.exception("kb_search_failed", node_id=node.id)
            await self._emit(
                protocol.Error(
                    code="kb_unavailable",
                    message="The knowledge base is unavailable; the assistant said so.",
                    retryable=True,
                )
            )
            return await self._say(node.data.not_found_reply, state, timer)

        log.info(
            "kb_search",
            node_id=node.id,
            hits=len(result.hits),
            best_similarity=None
            if result.best_similarity is None
            else round(result.best_similarity, 3),
        )
        if not result.found:
            return await self._say(node.data.not_found_reply, state, timer)

        reply = await self._loop.run(
            state, timer, system=[KB_SYSTEM_PROMPT, fragments_prompt(result.hits)]
        )
        return KBReply(
            text=reply.text, sources=cited_sources(reply.text, result.hits), usage=reply.usage
        )

    async def _say(self, text: str, state: SessionState, timer: TurnTimer) -> KBReply:
        timer.first_text()
        await self._emit(protocol.AssistantDelta(turn_id=state.turn, text=text))
        message = Message(role="assistant", content=text)
        state.history.append(HistoryEntry(turn=state.turn, message=message))
        return KBReply(text=text)


def _last_user_text(state: SessionState) -> str:
    for entry in reversed(state.history):
        if entry.message.role == "user":
            return entry.message.content
    return ""


# --- the tool ---------------------------------------------------------------------------


class SearchArgs(BaseModel):
    query: str = Field(
        min_length=2,
        max_length=500,
        description="Питання абонента своїми словами, з назвою тарифу чи послуги, якщо є.",
    )


def knowledge_tools(
    kb: KnowledgeSearch, *, top_k: int = 5, min_similarity: float = 0.35
) -> list[Tool[SearchArgs]]:
    """``search_knowledge_base``: the same search as ``kb_answer``, for ``llm_prompt`` nodes."""

    async def search(args: SearchArgs, _: ToolContext) -> ToolResult:
        result = await kb.search(args.query, top_k=top_k, min_similarity=min_similarity)
        return {
            "found": result.found,
            "fragments": [
                {"n": n, "document": hit.document_title, "section": hit.section, "text": hit.text}
                for n, hit in enumerate(result.hits, start=1)
            ],
        }

    return [
        Tool(
            name=SEARCH_TOOL,
            description=(
                "Шукає в базі знань Hvylia Mobile умови й ціни тарифів, послуг, роумінгу та "
                "відповіді на часті питання. Відповідай лише з повернутих фрагментів; "
                "found=false означає, що в базі такої інформації немає."
            ),
            args_model=SearchArgs,
            handler=search,
        )
    ]
