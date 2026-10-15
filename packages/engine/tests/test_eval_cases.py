"""Format and coverage checks for the eval set in `evals/cases/` (format: `evals/README.md`)."""

import re
from collections import Counter
from pathlib import Path

import pytest

from rozmova_engine.evals.cases import EvalCase, fast_subset, load_cases

ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = ROOT / "evals" / "cases"
KB_DIR = ROOT / "kb"


def _kb_service_ids() -> set[str]:
    readme = (KB_DIR / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| service \| `(\w+)` \|", readme, flags=re.MULTILINE))


def _kb_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in KB_DIR.rglob("*.md"))


CASES = load_cases(CASES_DIR)


def test_set_size_and_unique_ids() -> None:
    assert 40 <= len(CASES) <= 50
    ids = Counter(case.id for case in CASES)
    assert [case_id for case_id, count in ids.items() if count > 1] == []


def test_tag_coverage_meets_ticket() -> None:
    tags = Counter(tag for case in CASES for tag in case.tags)

    assert tags["injection"] >= 8
    assert tags["confirmation"] >= 6
    assert tags["out_of_scope"] >= 6
    assert tags["balance"] >= 1
    assert tags["tariff"] >= 1


def test_fast_subset_covers_every_tag() -> None:
    cases = CASES
    fast = fast_subset(cases)

    assert len(fast) < len(cases) / 2
    assert {tag for case in cases for tag in case.tags} == {t for c in fast for t in c.tags}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_case_is_consistent(case: EvalCase) -> None:
    services = _kb_service_ids()
    activations = [
        call
        for turn in case.turns
        for call in turn.expected_tools
        if call.name == "activate_service"
    ]

    for call in activations:
        assert set(call.args) == {"service_id"}
        assert call.args["service_id"] in services
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
    for case in CASES:
        for turn in case.turns:
            for fact in turn.expected_facts:
                assert set(re.findall(r"\d+(?:,\d+)?", fact)) <= kb_numbers, (case.id, fact)
