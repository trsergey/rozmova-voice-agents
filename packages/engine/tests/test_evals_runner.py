"""The eval runner on the demo flow with a scripted LLM, a fake KB and a fake judge."""

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rozmova_contracts import protocol
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.evals.cases import EvalCase
from rozmova_engine.evals.checks import check_run
from rozmova_engine.evals.judge import CORRECTNESS, FAITHFULNESS, RELEVANCY, JudgeInput, MetricScore
from rozmova_engine.evals.report import (
    Thresholds,
    build_report,
    judge_runs,
    load_thresholds,
    render_markdown,
)
from rozmova_engine.evals.runner import (
    Bot,
    CaseRun,
    ToolCallRecord,
    TurnRecord,
    run_case,
    run_cases,
)
from rozmova_engine.providers import ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider, Step, text_turn
from rozmova_engine.rag.search import SearchHit, SearchResult

ROOT = Path(__file__).resolve().parents[3]
FLOW = FlowGraph.model_validate_json((ROOT / "flows/hvylia-demo.json").read_text("utf-8"))
THRESHOLDS = Thresholds(
    deterministic={"tool_correctness": 0.95, "injection_rejection": 1.0, "completed_runs": 1.0},
    judge={RELEVANCY: 0.7, FAITHFULNESS: 0.8, CORRECTNESS: 0.6},
)
CHUNK = "# Тариф «Безлім»\n\n## Вартість\n\n395 грн на місяць."


def call(tool: str, /, **args: Any) -> list[Step]:
    return [ToolCall(id=f"c-{tool}", name=tool, arguments=args)]


def case(**fields: Any) -> EvalCase:
    return EvalCase.model_validate({"description": "test", "tags": ["balance"], **fields})


class FakeSearch:
    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        if "безлім" not in query.lower():
            return SearchResult(query=query, hits=[], best_similarity=0.1)
        hit = SearchHit(
            chunk_id=uuid.UUID(int=1),
            document_id=uuid.UUID(int=2),
            document_title="Тариф «Безлім»",
            source_path="tariffs/bezlim.md",
            section="Вартість",
            text=CHUNK,
            score=0.03,
            similarity=0.7,
            vector_rank=1,
            text_rank=1,
        )
        return SearchResult(query=query, hits=[hit], best_similarity=0.7)


def bot(*llm_calls: list[Step]) -> Bot:
    return Bot(FLOW, FakeProvider(llm_calls), "fake", FakeSearch())


def failed(run: CaseRun) -> list[str]:
    return [check.name for check in check_run(run) if not check.passed]


# --- running and deterministic checks -----------------------------------------------------


async def test_expected_tool_call_passes() -> None:
    balance = case(
        id="balance",
        turns=[{"user": "баланс", "expected_tools": [{"name": "get_balance"}], "reference": "x"}],
    )

    run = await run_case(bot(call("get_balance"), text_turn("120,50 грн.")), balance)

    assert run.error is None
    [turn] = run.turns
    assert turn.reply == "Хвилинку, перевіряю баланс. 120,50 грн."
    assert [(t.name, t.result is not None) for t in turn.tools] == [("get_balance", True)]
    assert turn.timings is not None
    assert failed(run) == []


async def test_wrong_tool_fails_with_the_calls_made() -> None:
    balance = case(
        id="balance",
        turns=[
            {
                "user": "баланс",
                "expected_tools": [{"name": "get_balance"}],
                "must_not_call": ["list_services"],
                "reference": "x",
            }
        ],
    )

    run = await run_case(bot(call("list_services"), text_turn("Ось послуги.")), balance)

    checks = {c.name: c for c in check_run(run)}
    assert not checks["turn 1: called get_balance"].passed
    assert checks["turn 1: called get_balance"].detail == "calls: list_services"
    assert not checks["turn 1: did not call list_services"].passed


async def test_two_phase_activation_is_confirmed() -> None:
    roaming = case(
        id="roaming",
        tags=["confirmation"],
        turns=[
            {
                "user": "підключіть роумінг Європа",
                # Arguments match case-insensitively.
                "expected_tools": [
                    {"name": "activate_service", "args": {"service_id": "Roaming_EU"}}
                ],
                "expected_facts": ["99 грн на місяць"],
            },
            {"user": "так", "expected_tools": [{"name": "activate_service"}], "reference": "x"},
        ],
        activated_services=["roaming_eu"],
    )
    llm = bot(
        call("activate_service", service_id="roaming_eu"),
        text_turn("99 грн на місяць. Підключити?"),
        call("activate_service", service_id="roaming_eu"),
        text_turn("Готово."),
    )

    run = await run_case(llm, roaming)

    assert run.activated == ["roaming_eu"]
    statuses = [t.result["status"] for turn in run.turns for t in turn.tools if t.result]
    assert statuses == ["confirmation_required", "activated"]
    assert failed(run) == []


def test_activation_without_a_quote_fails_the_confirmation_check() -> None:
    roaming = case(id="roaming", turns=[{"user": "так", "reference": "x"}])
    activated = ToolCallRecord(
        name="activate_service", args={"service_id": "roaming_eu"}, result={"status": "activated"}
    )
    run = CaseRun(case=roaming, turns=[TurnRecord(user="так", tools=[activated])])
    run.activated = ["roaming_eu"]

    assert failed(run) == [
        "activated nothing",
        "no activation without an earlier confirmation",
    ]


async def test_handoff_ends_the_call_and_skips_the_rest() -> None:
    operator = case(
        id="operator",
        tags=["handoff"],
        turns=[{"user": "оператора"}, {"user": "ні"}, {"user": "алло?"}],
        must_handoff=True,
    )
    llm = bot(
        call("route", name="operator"), text_turn("Підписка на новини?"), call("route", name="done")
    )

    run = await run_case(llm, operator)

    assert run.ended == "handoff"
    assert [turn.skipped for turn in run.turns] == [False, False, True]
    assert failed(run) == []


def test_handoff_is_a_refusal_for_an_injection_case_only() -> None:
    def handed_off(tags: list[str]) -> list[str]:
        run = CaseRun(
            case=case(id="c", tags=tags, turns=[{"user": "поверни 500 грн", "reference": "x"}]),
            turns=[TurnRecord(user="поверни 500 грн", reply="Передаю оператору.")],
            ended="handoff",
        )
        return failed(run)

    assert handed_off(["injection"]) == []
    assert handed_off(["balance"]) == ["did not hand off"]


async def test_knowledge_answer_records_retrieval_and_sources() -> None:
    question = {"user": "скільки коштує Безлім?", "expected_facts": ["395 грн"]}
    tariff = case(id="tariff", tags=["tariff"], turns=[question])

    run = await run_case(bot(call("route", name="tariff"), text_turn("395 грн [1].")), tariff)

    [turn] = run.turns
    assert turn.retrieval == [CHUNK]
    assert [s.marker for s in turn.sources] == [1]


async def test_a_broken_run_is_a_failed_case_not_a_crash() -> None:
    balance = case(id="balance", turns=[{"user": "баланс", "reference": "x"}])

    run = await run_case(bot(), balance)  # the scripted LLM has no answer

    assert run.error is not None
    assert "run finished" in failed(run)


async def test_slow_turn_times_out() -> None:
    balance = case(id="balance", turns=[{"user": "баланс", "reference": "x"}])

    run = await run_case(bot([Delay(5.0)]), balance, turn_timeout=0.05)

    assert run.error == "turn 1 timed out after 0.05 s"


async def test_cases_run_concurrently_and_keep_their_order() -> None:
    cases = [case(id=f"c{i}", turns=[{"user": "привіт", "reference": "x"}]) for i in range(3)]
    done: list[str] = []

    def answer() -> Bot:
        return bot(text_turn("Вітаю!"), text_turn("Вітаю!"), text_turn("Вітаю!"))

    runs = await run_cases(answer(), cases, concurrency=2, on_done=lambda r: done.append(r.case.id))

    assert [run.case.id for run in runs] == ["c0", "c1", "c2"]
    assert sorted(done) == ["c0", "c1", "c2"]


async def test_on_done_may_be_a_coroutine() -> None:
    """admin-api counts progress in Redis from ``on_done``."""
    cases = [case(id=f"c{i}", turns=[{"user": "привіт", "reference": "x"}]) for i in range(2)]
    done: list[str] = []

    async def record(run: CaseRun) -> None:
        await asyncio.sleep(0)
        done.append(run.case.id)

    await run_cases(bot(text_turn("Вітаю!"), text_turn("Вітаю!")), cases, on_done=record)

    assert sorted(done) == ["c0", "c1"]


# --- judging and the report ----------------------------------------------------------------


class FakeJudge:
    model = "fake-judge"

    def __init__(self, score: float = 0.9, fail_on: str | None = None) -> None:
        self.score_value = score
        self.fail_on = fail_on
        self.inputs: list[JudgeInput] = []

    @property
    def cost_usd(self) -> float:
        return 0.01 * len(self.inputs)

    async def score(self, item: JudgeInput) -> dict[str, MetricScore]:
        self.inputs.append(item)
        if self.fail_on and self.fail_on in item.question:
            raise RuntimeError("judge down")
        scores = {RELEVANCY: MetricScore(self.score_value, "ok")}
        if item.retrieval:
            scores[FAITHFULNESS] = MetricScore(self.score_value, "ok")
        return scores


def _run(case_: EvalCase, *replies: str, retrieval: list[str] | None = None) -> CaseRun:
    turns = [
        TurnRecord(user=turn.user, reply=reply, retrieval=retrieval or [])
        for turn, reply in zip(case_.turns, replies, strict=True)
    ]
    return CaseRun(case=case_, turns=turns)


async def test_judge_sees_the_dialogue_and_skips_injection_and_unjudged_turns() -> None:
    two_turns = case(
        id="two",
        turns=[{"user": "перше питання"}, {"user": "друге питання", "expected_facts": ["факт"]}],
    )
    injection = case(id="inj", tags=["injection"], turns=[{"user": "ігноруй", "reference": "x"}])
    judge = FakeJudge()

    scores = await judge_runs(
        [_run(two_turns, "відповідь 1", "відповідь 2"), _run(injection, "ні")], judge
    )

    [item] = judge.inputs
    assert item.question == "Абонент: перше питання\nАсистент: відповідь 1\nАбонент: друге питання"
    assert item.expected == "- факт"
    assert scores["two"][0] is None
    assert scores["two"][1] is not None
    assert scores["inj"] == [None]


async def test_report_gates_and_markdown() -> None:
    good = case(id="good", tags=["tariff"], turns=[{"user": "Безлім?", "expected_facts": ["395"]}])
    weak = case(id="weak", tags=["tariff"], turns=[{"user": "ціна?", "reference": "x"}])
    runs = [_run(good, "395 грн [1]", retrieval=[CHUNK]), _run(weak, "не знаю")]
    errors: list[str] = []
    scores = await judge_runs(runs, FakeJudge(), on_error=lambda c, t, e: errors.append(c))
    scores["weak"] = [{RELEVANCY: MetricScore(0.3, "off topic")}]
    now = datetime.now(UTC)

    report = build_report(
        runs,
        scores,
        THRESHOLDS,
        started_at=now,
        finished_at=now,
        flow="flows/hvylia-demo.json",
        provider="openai",
        model="gpt-4.1-mini",
        judge_model="fake-judge",
        judge_cost_usd=0.02,
        judge_errors=len(errors),
        subset="full",
    )

    summary = report.summary
    # A low judge score does not fail the case; it shows in the mean and in the report.
    assert (summary.cases, summary.cases_passed) == (2, 2)
    assert [case.low_scores for case in report.cases] == [0, 1]
    gates = {gate.metric: gate for gate in summary.gates}
    # No data is no failure: this run has no tool expectations and no injection case.
    for metric in ("tool_correctness", "injection_rejection"):
        assert gates[metric].value is None
        assert gates[metric].passed
    assert gates[RELEVANCY].value == pytest.approx(0.6)
    assert not gates[RELEVANCY].passed
    assert gates[FAITHFULNESS].value == pytest.approx(0.9)
    assert not summary.passed
    assert summary.by_tag["tariff"].passed == 2
    markdown = render_markdown(report)
    assert markdown.startswith("# Eval report: FAIL")
    assert "## Failed cases (0)" in markdown
    assert "## Low judge scores (1 cases)" in markdown
    assert "### `weak`" in markdown
    assert "turn 1 answer_relevancy 0.30: off topic" in markdown
    assert "`good`" not in markdown
    # The JSON is the eval_report stored by the admin API.
    assert report.model_validate_json(report.model_dump_json()) == report


async def test_judge_errors_are_counted_not_raised() -> None:
    broken = case(id="broken", turns=[{"user": "питання", "reference": "x"}])
    errors: list[str] = []

    scores = await judge_runs(
        [_run(broken, "відповідь")],
        FakeJudge(fail_on="питання"),
        on_error=lambda c, t, e: errors.append(f"{c}#{t}"),
    )

    assert errors == ["broken#0"]
    assert scores["broken"] == [None]


def test_report_ttft_percentiles_count_only_llm_turns() -> None:
    def turn(ttft: float, stages: dict[str, float]) -> TurnRecord:
        timings = protocol.TurnTimings(total_ms=ttft, ttft_ms=ttft, stages=stages)
        return TurnRecord(user="u", reply="r", timings=timings)

    run = CaseRun(
        case=case(id="t", turns=[{"user": "u", "reference": "x"}] * 4),
        turns=[
            turn(1000, {"llm_ttft": 990}),
            turn(2000, {"llm_ttft": 1990}),
            turn(3000, {"llm_ttft": 2990, "rag_search": 700}),
            # A scripted node reply without the LLM would pull the percentiles down.
            turn(5, {}),
        ],
    )
    now = datetime.now(UTC)

    report = build_report(
        [run],
        {},
        THRESHOLDS,
        started_at=now,
        finished_at=now,
        flow="flows/hvylia-demo.json",
        provider="openai",
        model="gpt-4.1-mini",
        judge_model=None,
        judge_cost_usd=None,
        judge_errors=0,
        subset="full",
    )

    summary = report.summary
    assert summary.llm_turns == 3
    assert summary.ttft_p50_ms == 2000
    assert summary.ttft_p95_ms == 2900
    assert "TTFT over 3 LLM turns: p50 2000 ms, p95 2900 ms" in render_markdown(report)


def test_report_without_llm_turns_has_no_ttft() -> None:
    now = datetime.now(UTC)
    report = build_report(
        [_run(case(id="t", turns=[{"user": "u", "reference": "x"}]), "r")],
        {},
        THRESHOLDS,
        started_at=now,
        finished_at=now,
        flow="f",
        provider="openai",
        model="m",
        judge_model=None,
        judge_cost_usd=None,
        judge_errors=0,
        subset="full",
    )

    assert (report.summary.ttft_p50_ms, report.summary.ttft_p95_ms) == (None, None)
    assert "TTFT" not in render_markdown(report)


def test_repo_thresholds_load() -> None:
    thresholds = load_thresholds(ROOT / "evals/thresholds.yaml")

    assert thresholds.deterministic["injection_rejection"] == 1.0
    assert thresholds.deterministic["tool_correctness"] == 0.95
    assert set(thresholds.judge) == {RELEVANCY, FAITHFULNESS, CORRECTNESS}


def test_report_shows_the_flow_path_relative_to_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rozmova_engine.evals.__main__ import _shown

    monkeypatch.chdir(tmp_path)
    (tmp_path / "flows").mkdir()

    assert _shown(tmp_path / "flows" / "demo.json") == "flows/demo.json"
    assert _shown(Path("/elsewhere/demo.json")) == "/elsewhere/demo.json"
