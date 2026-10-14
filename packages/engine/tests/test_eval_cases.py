"""Format and coverage checks for the eval set in `evals/cases/` (format: `evals/README.md`).

The eval runner (ROZ-64) will own the case model; until then this test pins the format.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = ROOT / "evals" / "cases"
KB_DIR = ROOT / "kb"

# SDD "Дії MVP (моки)".
ToolName = Literal[
    "get_balance",
    "get_current_tariff",
    "list_services",
    "activate_service",
    "search_knowledge_base",
]
Tag = Literal["balance", "tariff", "faq", "out_of_scope", "injection", "confirmation", "handoff"]


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


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    tags: list[Tag] = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1)
    must_handoff: bool = False
    activated_services: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_expectation(self) -> "EvalCase":
        judged = any(turn.expected_facts or turn.reference for turn in self.turns)
        if not judged and not self.must_handoff:
            raise ValueError("a case needs expected_facts, a reference answer or must_handoff")
        return self


def _load_cases() -> list[tuple[str, EvalCase]]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, list), path.name
        cases += [(path.name, EvalCase.model_validate(item)) for item in raw]
    return cases


def _kb_service_ids() -> set[str]:
    readme = (KB_DIR / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| service \| `(\w+)` \|", readme, flags=re.MULTILINE))


def _kb_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in KB_DIR.rglob("*.md"))


CASES = _load_cases()


def test_set_size_and_unique_ids() -> None:
    assert 40 <= len(CASES) <= 50
    ids = Counter(case.id for _, case in CASES)
    assert [case_id for case_id, count in ids.items() if count > 1] == []


def test_tag_coverage_meets_ticket() -> None:
    tags = Counter(tag for _, case in CASES for tag in case.tags)

    assert tags["injection"] >= 8
    assert tags["confirmation"] >= 6
    assert tags["out_of_scope"] >= 6
    assert tags["balance"] >= 1
    assert tags["tariff"] >= 1


@pytest.mark.parametrize(("file", "case"), CASES, ids=lambda value: getattr(value, "id", ""))
def test_case_is_consistent(file: str, case: EvalCase) -> None:
    services = _kb_service_ids()
    activations = [
        call
        for turn in case.turns
        for call in turn.expected_tools
        if call.name == "activate_service"
    ]

    for call in activations:
        assert set(call.args) == {"service_id"}, file
        assert call.args["service_id"] in services, file
    assert set(case.activated_services) <= {call.args["service_id"] for call in activations}
    if "confirmation" in case.tags and case.activated_services:
        # Two-phase activation needs at least a request turn and a consent turn (ADR 0013).
        assert len(case.turns) >= 2
    if "injection" in case.tags:
        assert case.activated_services == []
        forbidden = {name for turn in case.turns for name in turn.must_not_call}
        # A confirmation-style injection may legitimately reach phase one of the activation.
        assert "activate_service" in forbidden or "confirmation" in case.tags


def test_fact_numbers_come_from_kb() -> None:
    """Every number in an expected fact must appear in the knowledge base (no invented prices)."""
    kb_numbers = set(re.findall(r"\d+(?:,\d+)?", _kb_text()))
    for _, case in CASES:
        for turn in case.turns:
            for fact in turn.expected_facts:
                assert set(re.findall(r"\d+(?:,\d+)?", fact)) <= kb_numbers, (case.id, fact)
