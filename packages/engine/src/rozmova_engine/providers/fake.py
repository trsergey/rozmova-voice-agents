"""Scripted LLM provider for tests: no network, deterministic events and timing.

Each ``stream()`` call plays the next scripted turn. A turn is a list of steps: stream
events are yielded, ``Delay`` sleeps (so timeouts and cancellation behave as with a real
model) and ``Raise`` fails the stream at that point.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Iterable, Sequence
from dataclasses import dataclass

from rozmova_engine.providers.base import Message, StreamEvent, TextDelta, ToolSpec, Usage


@dataclass(frozen=True, slots=True)
class Delay:
    """Sleep before the next step, e.g. to simulate time to first token."""

    seconds: float


@dataclass(frozen=True, slots=True)
class Raise:
    """Raise ``error`` from the stream at this point."""

    error: Exception


Step = StreamEvent | Delay | Raise


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """What the engine sent to the model in one ``stream()`` call."""

    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    model: str


class FakeScriptExhaustedError(AssertionError):
    """The engine called the model more times than the test scripted."""


class FakeProvider:
    """An ``LLMProvider`` that replays scripted turns and records every call."""

    def __init__(self, turns: Iterable[Sequence[Step]] = ()) -> None:
        self._turns: deque[tuple[Step, ...]] = deque(tuple(turn) for turn in turns)
        self.calls: list[RecordedCall] = []

    def add_turn(self, *steps: Step) -> None:
        self._turns.append(steps)

    @property
    def remaining_turns(self) -> int:
        return len(self._turns)

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec], *, model: str
    ) -> AsyncGenerator[StreamEvent]:
        self.calls.append(RecordedCall(tuple(messages), tuple(tools), model))
        if not self._turns:
            raise FakeScriptExhaustedError(f"no scripted turn left for call #{len(self.calls)}")
        for step in self._turns.popleft():
            match step:
                case Delay(seconds):
                    await asyncio.sleep(seconds)
                case Raise(error):
                    raise error
                case _:
                    yield step


def text_turn(
    text: str, *, chunk_size: int = 8, delay: float = 0.0, usage: Usage | None = None
) -> list[Step]:
    """Script a plain text answer split into deltas of ``chunk_size`` characters.

    ``delay`` is inserted before every delta; ``usage`` is appended at the end if given.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    steps: list[Step] = []
    for start in range(0, len(text), chunk_size):
        if delay:
            steps.append(Delay(delay))
        steps.append(TextDelta(text=text[start : start + chunk_size]))
    if usage is not None:
        steps.append(usage)
    return steps
