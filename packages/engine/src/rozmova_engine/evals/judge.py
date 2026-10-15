"""LLM-judged metrics (ADR 0007): DeepEval answer relevancy, faithfulness and correctness.

- ``answer_relevancy``: the reply addresses the subscriber's message (in its dialogue).
- ``faithfulness``: every claim in the reply is backed by the retrieved chunks; only for
  turns that retrieved something.
- ``correctness`` (G-Eval): the reply states the case's ``expected_facts`` and does what
  its ``reference`` describes; the two metrics above never look at them.

DeepEval is the optional ``evals`` extra and is imported only when a judge is created, so
the services and ``make test`` never load it.
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

RELEVANCY = "answer_relevancy"
FAITHFULNESS = "faithfulness"
CORRECTNESS = "correctness"

# G-Eval steps; the dialogue is Ukrainian, the steps are for the judge model.
CORRECTNESS_STEPS = [
    "The expected output is a list of facts (lines starting with '-') that the reply must "
    "state, and/or a reference: either a model answer or a description of the expected "
    "behaviour.",
    "Check that each listed fact appears in the actual output with the same numbers and "
    "names; the wording may differ.",
    "Check that the actual output does what the reference does or describes, for example "
    "asks for confirmation, refuses, gives a price or offers an operator.",
    "Do not penalize extra details, polite phrases, follow-up questions, or account values "
    "(balance, current tariff) that the reference only describes.",
    "Penalize missing facts, facts or prices that contradict the expected output, and an "
    "action other than the one the reference expects.",
]


@dataclass(frozen=True, slots=True)
class JudgeInput:
    """``question`` is the dialogue so far, ending with the subscriber's message."""

    question: str
    answer: str
    expected: str
    retrieval: list[str]


@dataclass(frozen=True, slots=True)
class MetricScore:
    score: float
    reason: str | None = None


class Judge(Protocol):
    model: str

    async def score(self, item: JudgeInput) -> dict[str, MetricScore]:
        """Scores in 0..1 for the metrics that apply to ``item``."""
        ...

    @property
    def cost_usd(self) -> float:
        """What the judge's calls cost so far, as DeepEval estimates it."""
        ...


class DeepEvalJudge:
    """A judge on an OpenAI model, or on Claude for a ``claude-*`` model name (the key
    then comes from ``ANTHROPIC_API_KEY``)."""

    def __init__(self, model: str, *, api_key: SecretStr | None = None) -> None:
        # Before the first deepeval import: no usage reports leave the machine.
        os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
        from deepeval.models import AnthropicModel, OpenAIModel

        self.model = model
        key = api_key.get_secret_value() if api_key else None
        self._llm: Any = (
            AnthropicModel(model=model, temperature=0)
            if model.startswith("claude")
            else OpenAIModel(model=model, api_key=key, temperature=0)
        )
        self._cost = 0.0

    @property
    def cost_usd(self) -> float:
        return self._cost

    async def score(self, item: JudgeInput) -> dict[str, MetricScore]:
        from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
        from deepeval.test_case import LLMTestCase, SingleTurnParams

        retrieval: list[Any] = list(item.retrieval)
        case = LLMTestCase(
            input=item.question,
            actual_output=item.answer,
            expected_output=item.expected or None,
            retrieval_context=retrieval or None,
        )
        # Thresholds are ours (evals/thresholds.yaml); DeepEval's pass flag is not used.
        metrics: dict[str, Any] = {RELEVANCY: AnswerRelevancyMetric(model=self._llm, threshold=0)}
        if item.retrieval:
            metrics[FAITHFULNESS] = FaithfulnessMetric(model=self._llm, threshold=0)
        if item.expected:
            metrics[CORRECTNESS] = GEval(
                name="Correctness",
                evaluation_steps=CORRECTNESS_STEPS,
                evaluation_params=[
                    SingleTurnParams.INPUT,
                    SingleTurnParams.ACTUAL_OUTPUT,
                    SingleTurnParams.EXPECTED_OUTPUT,
                ],
                model=self._llm,
                threshold=0,
            )
        await asyncio.gather(
            *(metric.a_measure(case, _show_indicator=False) for metric in metrics.values())
        )
        self._cost += sum(metric.evaluation_cost or 0.0 for metric in metrics.values())
        return {
            name: MetricScore(score=float(metric.score), reason=metric.reason)
            for name, metric in metrics.items()
        }
