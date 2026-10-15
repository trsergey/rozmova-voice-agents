"""The kb_answer node and the search_knowledge_base tool over a scripted LLM and a fake KB."""

import asyncio
import uuid
from uuid import uuid4

import pytest

from rozmova_contracts import protocol
from rozmova_contracts.flow import KBAnswerData, KBAnswerNode
from rozmova_engine.providers import Message, ToolCall, Usage
from rozmova_engine.providers.fake import Delay, FakeProvider, text_turn
from rozmova_engine.rag.answer import (
    FRAGMENTS_HEADER,
    KB_SYSTEM_PROMPT,
    SEARCH_TOOL,
    KnowledgeBaseAnswerer,
    cited_sources,
    fragments_prompt,
    knowledge_tools,
)
from rozmova_engine.rag.search import SearchHit, SearchResult
from rozmova_engine.session import HistoryEntry, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import ToolContext, ToolRegistry, call_tool

NOT_FOUND = "У базі знань немає відповіді на це питання. Хочете, переведу на оператора?"
NODE = KBAnswerNode(
    id="tariff_kb",
    type="kb_answer",
    data=KBAnswerData(top_k=3, min_similarity=0.4, not_found_reply=NOT_FOUND),
)
CTX = ToolContext(subscriber_id="demo")


def hit(n: int, title: str = "Тариф «Безлім»", section: str = "Вартість") -> SearchHit:
    return SearchHit(
        chunk_id=uuid.UUID(int=n),
        document_id=uuid.UUID(int=100 + n),
        document_title=title,
        source_path="tariffs/bezlim.md",
        section=section,
        text=f"# {title}\n\n## {section}\n\nФакт номер {n}.",
        score=0.03,
        similarity=0.6,
        vector_rank=n,
        text_rank=n,
    )


HITS = [hit(1), hit(2, section="Що входить"), hit(3, title="FAQ", section="Баланс")]


class FakeSearch:
    def __init__(self, hits: list[SearchHit] | None = None, error: Exception | None = None):
        self.hits = hits or []
        self.error = error
        self.calls: list[tuple[str, int, float]] = []

    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        self.calls.append((query, top_k, min_similarity))
        if self.error is not None:
            raise self.error
        best = 0.6 if self.hits else 0.2
        return SearchResult(query=query, hits=self.hits, best_similarity=best)


class Node:
    """A kb_answer node of one session: scripted LLM, fake search, recorded events."""

    def __init__(self, search: FakeSearch, provider: FakeProvider | None = None) -> None:
        self.search = search
        self.provider = provider or FakeProvider()
        self.events: list[protocol.ProtocolMessage] = []
        self.spoke = asyncio.Event()
        self.state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1, turn=2)
        self.timer = TurnTimer()
        loop = ToolLoop(self.provider, "fake", ToolRegistry(), CTX, self.emit)
        self.answerer = KnowledgeBaseAnswerer(search, loop, self.emit)

    async def emit(self, event: protocol.ProtocolMessage) -> None:
        self.events.append(event)
        if isinstance(event, protocol.AssistantDelta):
            self.spoke.set()

    async def ask(self, text: str):  # type: ignore[no-untyped-def]
        message = Message(role="user", content=text)
        self.state.history.append(HistoryEntry(turn=self.state.turn, message=message))
        return await self.answerer.answer(NODE, self.state, self.timer)

    def said(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, protocol.AssistantDelta))


# --- markers → sources ------------------------------------------------------------------


def test_cited_sources_follow_the_markers() -> None:
    sources = cited_sources("Ціна 395 грн [2]. Інтернет безлімітний [1][2], деталі [1, 3].", HITS)

    assert [s.marker for s in sources] == [2, 1, 3]
    first = sources[0]
    assert first.chunk_id == str(uuid.UUID(int=2))
    assert first.document_id == str(uuid.UUID(int=102))
    assert first.document_title == "Тариф «Безлім»"
    assert first.section == "Що входить"


@pytest.mark.parametrize("text", ["Без маркерів.", "Вигаданий [4] і нульовий [0].", "[x] [1a]"])
def test_text_without_valid_markers_cites_nothing(text: str) -> None:
    assert cited_sources(text, HITS) == []


def test_fragments_are_numbered_from_one() -> None:
    prompt = fragments_prompt(HITS)

    assert prompt.startswith(FRAGMENTS_HEADER)
    assert "[1] # Тариф «Безлім»\n\n## Вартість\n\nФакт номер 1." in prompt
    assert "[3] # FAQ" in prompt


# --- the node ---------------------------------------------------------------------------


async def test_answer_streams_the_reply_and_cites_its_sources() -> None:
    usage = Usage(input_tokens=300, output_tokens=20)
    answer = "Безлім коштує 395 грн [1] і має безліміт [2]."
    provider = FakeProvider([text_turn(answer, usage=usage)])
    node = Node(FakeSearch(HITS), provider)

    reply = await node.ask("скільки коштує Безлім?")

    assert node.search.calls == [("скільки коштує Безлім?", 3, 0.4)]
    assert node.said() == reply.text == "Безлім коштує 395 грн [1] і має безліміт [2]."
    assert [s.marker for s in reply.sources] == [1, 2]
    assert reply.usage == usage
    # The LLM got the rules and the numbered chunks as system messages, then the dialogue.
    [call] = provider.calls
    assert [m.role for m in call.messages] == ["system", "system", "user"]
    assert call.messages[0].content == KB_SYSTEM_PROMPT
    assert call.messages[1].content == fragments_prompt(HITS)
    assert call.tools == ()
    # The reply is recorded once, by the answerer.
    assert [e.message.content for e in node.state.history if e.message.role == "assistant"] == [
        reply.text
    ]
    assert "rag_search" in node.timer.stages


async def test_nothing_found_says_the_not_found_reply_without_the_llm() -> None:
    node = Node(FakeSearch([]))

    reply = await node.ask("що входить у тариф Космос?")

    assert reply.text == node.said() == NOT_FOUND
    assert reply.sources == []
    assert node.provider.calls == []
    assert node.state.history[-1].message.content == NOT_FOUND
    assert node.timer.to_timings().ttft_ms is not None


async def test_search_failure_is_a_typed_error_and_the_not_found_reply() -> None:
    node = Node(FakeSearch(error=TimeoutError()))

    reply = await node.ask("скільки коштує Безлім?")

    errors = [e for e in node.events if isinstance(e, protocol.Error)]
    assert [(e.code, e.retryable) for e in errors] == [("kb_unavailable", True)]
    assert reply.text == NOT_FOUND
    assert reply.sources == []
    assert node.provider.calls == []


async def test_search_is_not_shielded_from_cancellation() -> None:
    class Hanging(FakeSearch):
        async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    node = Node(Hanging())
    task = asyncio.create_task(node.ask("Безлім"))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(isinstance(e, protocol.Error) for e in node.events)


async def test_barge_in_keeps_the_heard_part_as_interrupted() -> None:
    heard = "Безлім коштує 395 грн [1]."
    provider = FakeProvider([[*text_turn(heard, chunk_size=len(heard)), Delay(10.0)]])
    node = Node(FakeSearch(HITS), provider)
    task = asyncio.create_task(node.ask("скільки коштує Безлім?"))
    await node.spoke.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    last = node.state.history[-1]
    assert last.interrupted
    assert last.message.content == heard


# --- the tool ---------------------------------------------------------------------------


async def test_search_tool_returns_numbered_fragments() -> None:
    search = FakeSearch(HITS[:2])
    registry = ToolRegistry(knowledge_tools(search, top_k=4, min_similarity=0.3))
    state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1, turn=1)
    call = ToolCall(id="c1", name=SEARCH_TOOL, arguments={"query": "роумінг Європа"})

    result = await call_tool(registry, call, CTX, state)

    assert search.calls == [("роумінг Європа", 4, 0.3)]
    assert result["found"] is True
    assert result["fragments"] == [
        {"n": 1, "document": "Тариф «Безлім»", "section": "Вартість", "text": HITS[0].text},
        {"n": 2, "document": "Тариф «Безлім»", "section": "Що входить", "text": HITS[1].text},
    ]
    assert not registry.has_side_effect(SEARCH_TOOL)


async def test_search_tool_reports_not_found_and_failures() -> None:
    state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1, turn=1)
    call = ToolCall(id="c1", name=SEARCH_TOOL, arguments={"query": "тариф Космос"})

    empty = await call_tool(ToolRegistry(knowledge_tools(FakeSearch([]))), call, CTX, state)
    broken = await call_tool(
        ToolRegistry(knowledge_tools(FakeSearch(error=OSError("db down")))), call, CTX, state
    )

    assert empty == {"found": False, "fragments": []}
    assert broken["error"] == "tool_failed"
