"""Per-turn latency accounting for ``turn_end.timings`` (SDD "Бюджет затримки").

One ``TurnTimer`` lives for one turn. The gateway starts it when it accepts ``user_text``;
the engine records stages into it; ``ttft_ms`` is end to end, from that moment to the
first ``assistant_delta``, which is the TTFT the PRD budgets.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rozmova_contracts.protocol import TurnTimings


class TurnTimer:
    """Stage durations in milliseconds; a stage measured twice adds up."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._started = clock()
        self._first_text: float | None = None
        self.stages: dict[str, float] = {}
        # The type of the node running now, and of the one that sent the first text.
        self.node_type: str | None = None
        self.first_text_node_type: str | None = None

    def elapsed_ms(self) -> float:
        return (self._clock() - self._started) * 1000

    def add(self, stage: str, ms: float) -> None:
        self.stages[stage] = self.stages.get(stage, 0.0) + ms

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Measure the ``with`` block as ``name``, also when it raises or is cancelled."""
        started = self._clock()
        try:
            yield
        finally:
            self.add(name, (self._clock() - started) * 1000)

    def first_text(self) -> None:
        """Mark the first text sent to the subscriber; later calls are ignored."""
        if self._first_text is None:
            self._first_text = self._clock()
            self.first_text_node_type = self.node_type

    def to_timings(self) -> TurnTimings:
        ttft = None if self._first_text is None else (self._first_text - self._started) * 1000
        stages = {name: round(ms, 1) for name, ms in self.stages.items()}
        return TurnTimings(
            total_ms=round(self.elapsed_ms(), 1),
            ttft_ms=None if ttft is None else round(ttft, 1),
            stages=stages,
        )
