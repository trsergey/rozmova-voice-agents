"""The eval case format (``evals/README.md``) and loading of ``evals/cases/*.yaml``."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# SDD "Дії MVP (моки)".
ToolName = Literal[
    "get_balance",
    "get_current_tariff",
    "list_services",
    "activate_service",
    "search_knowledge_base",
]
Tag = Literal["balance", "tariff", "faq", "out_of_scope", "injection", "confirmation", "handoff"]

# Cases of `make eval-fast`: the first ones of each tag, in file order.
FAST_PER_TAG = 2


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedCall(_Strict):
    name: ToolName
    args: dict[str, Any] = Field(default_factory=dict)


class Turn(_Strict):
    user: str = Field(min_length=1)
    expected_tools: list[ExpectedCall] = Field(default_factory=list)
    must_not_call: list[ToolName] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list)
    reference: str | None = None

    @model_validator(mode="after")
    def _no_contradiction(self) -> "Turn":
        clash = {call.name for call in self.expected_tools} & set(self.must_not_call)
        if clash:
            raise ValueError(f"tools both expected and forbidden: {sorted(clash)}")
        return self

    @property
    def judged(self) -> bool:
        return bool(self.expected_facts or self.reference)

    def expected_output(self) -> str:
        """What the judge compares the reply with: the facts, then the reference."""
        parts = [f"- {fact}" for fact in self.expected_facts]
        if self.reference:
            parts.append(self.reference.strip())
        return "\n".join(parts)


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    tags: list[Tag] = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1)
    must_handoff: bool = False
    activated_services: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_expectation(self) -> "EvalCase":
        if not any(turn.judged for turn in self.turns) and not self.must_handoff:
            raise ValueError("a case needs expected_facts, a reference answer or must_handoff")
        return self


def load_cases(directory: Path) -> list[EvalCase]:
    """All cases of ``directory/*.yaml`` in file and list order; ids must be unique."""
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{path.name}: expected a list of cases")
        cases.extend(EvalCase.model_validate(item) for item in raw)
    duplicates = sorted({c.id for c in cases if sum(o.id == c.id for o in cases) > 1})
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return cases


def fast_subset(cases: Iterable[EvalCase], per_tag: int = FAST_PER_TAG) -> list[EvalCase]:
    """The first ``per_tag`` cases of every tag, keeping the original order."""
    taken: dict[str, int] = {}
    chosen: list[EvalCase] = []
    for case in cases:
        if any(taken.get(tag, 0) < per_tag for tag in case.tags):
            chosen.append(case)
            for tag in case.tags:
                taken[tag] = taken.get(tag, 0) + 1
    return chosen
