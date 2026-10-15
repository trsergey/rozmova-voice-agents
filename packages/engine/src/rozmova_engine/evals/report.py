"""Scoring, thresholds and the ``eval_report`` (JSON for the admin API, Markdown for people).

A case passes when all its deterministic checks pass: those are the hard guarantees
(ADR 0007). Judge scores are noisy, so they gate the run through their means; a turn
below a judge threshold is listed as a low score, not a failed case. The run passes when
every summary metric meets ``evals/thresholds.yaml``. The JSON shape is the
``eval_report`` that ROZ-73 stores in ``bots.draft_eval_report`` and
``bot_versions.eval_report``.
"""

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from rozmova_engine.evals.checks import Check, check_run
from rozmova_engine.evals.judge import Judge, JudgeInput, MetricScore
from rozmova_engine.evals.runner import CaseRun, TurnRecord

log = structlog.get_logger(__name__)

REPORT_FORMAT = 1
JUDGE_CONCURRENCY = 4
# Summary metrics from the deterministic checks, by Check.kind.
CHECK_METRICS = {
    "tool": "tool_correctness",
    "activation": "activation_accuracy",
    "confirmation": "confirmation_safety",
    "handoff": "handoff_accuracy",
}

TurnScores = dict[str, MetricScore] | None


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Shares of passed checks (0..1); ``injection_rejection`` is the share of injection
    # cases whose checks all passed, ``completed_runs`` of runs that did not break.
    deterministic: dict[str, float]
    # Mean judge scores (0..1); a turn below the same value fails its case.
    judge: dict[str, float] = Field(default_factory=dict)


def load_thresholds(path: Path) -> Thresholds:
    return Thresholds.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- the report model --------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportCheck(_Model):
    kind: str
    name: str
    passed: bool
    detail: str = ""


class ReportScore(_Model):
    score: float
    reason: str | None = None
    passed: bool


class ReportToolCall(_Model):
    name: str
    args: dict[str, JsonValue]
    result: dict[str, JsonValue] | None


class ReportTurn(_Model):
    user: str
    reply: str
    skipped: bool
    tools: list[ReportToolCall]
    sources: list[str]
    errors: list[str]
    scores: dict[str, ReportScore]
    ttft_ms: float | None
    total_ms: float | None


class ReportCase(_Model):
    id: str
    description: str
    tags: list[str]
    passed: bool
    low_scores: int
    checks: list[ReportCheck]
    turns: list[ReportTurn]
    ended: str | None
    activated: list[str]
    error: str | None


class Gate(_Model):
    metric: str
    value: float | None
    threshold: float
    passed: bool


class TagSummary(_Model):
    cases: int
    passed: int


class Summary(_Model):
    passed: bool
    cases: int
    cases_passed: int
    gates: list[Gate]
    by_tag: dict[str, TagSummary]
    input_tokens: int
    output_tokens: int
    judge_cost_usd: float | None
    judge_errors: int


class EvalReport(_Model):
    format: int = REPORT_FORMAT
    started_at: datetime
    finished_at: datetime
    flow: str
    provider: str
    model: str
    judge_model: str | None
    subset: str
    summary: Summary
    cases: list[ReportCase]


# --- judging -----------------------------------------------------------------------------


async def judge_runs(
    runs: Sequence[CaseRun],
    judge: Judge,
    *,
    concurrency: int = JUDGE_CONCURRENCY,
    on_error: Callable[[str, int, Exception], None] | None = None,
) -> dict[str, list[TurnScores]]:
    """Scores per case id and turn; ``None`` for a turn that is not judged.

    A turn is judged when it has an expected answer or retrieved chunks, and a reply.
    Injection cases and broken runs are checked deterministically only.
    """
    semaphore = asyncio.Semaphore(concurrency)
    scores: dict[str, list[TurnScores]] = {run.case.id: [None] * len(run.turns) for run in runs}

    async def one(run: CaseRun, index: int, item: JudgeInput) -> None:
        async with semaphore:
            try:
                scores[run.case.id][index] = await judge.score(item)
            except Exception as exc:  # a judge failure is reported, not fatal
                log.warning("eval_judge_failed", case_id=run.case.id, turn=index + 1)
                if on_error is not None:
                    on_error(run.case.id, index, exc)

    async with asyncio.TaskGroup() as group:
        for run in runs:
            if run.error is not None or "injection" in run.case.tags:
                continue
            for index, (turn, record) in enumerate(zip(run.case.turns, run.turns, strict=False)):
                if record.skipped or not record.reply or not (turn.judged or record.retrieval):
                    continue
                item = JudgeInput(
                    question=_dialogue(run.turns[: index + 1]),
                    answer=record.reply,
                    expected=turn.expected_output(),
                    retrieval=record.retrieval,
                )
                group.create_task(one(run, index, item))
    return scores


def _dialogue(turns: Sequence[TurnRecord]) -> str:
    """Earlier turns for context, then the message being answered."""
    if len(turns) == 1:
        return turns[0].user
    lines = []
    for turn in turns[:-1]:
        lines += [f"Абонент: {turn.user}", f"Асистент: {turn.reply}"]
    lines.append(f"Абонент: {turns[-1].user}")
    return "\n".join(lines)


# --- building the report -----------------------------------------------------------------


def build_report(
    runs: Sequence[CaseRun],
    scores: dict[str, list[TurnScores]],
    thresholds: Thresholds,
    *,
    started_at: datetime,
    finished_at: datetime,
    flow: str,
    provider: str,
    model: str,
    judge_model: str | None,
    judge_cost_usd: float | None,
    judge_errors: int,
    subset: str,
) -> EvalReport:
    cases: list[ReportCase] = []
    all_checks: list[Check] = []
    judged: dict[str, list[float]] = {}
    injection: list[bool] = []

    for run in runs:
        checks = check_run(run)
        all_checks += checks
        turn_scores = scores.get(run.case.id, [None] * len(run.turns))
        turns = [
            _report_turn(record, turn_scores[i] if i < len(turn_scores) else None, thresholds)
            for i, record in enumerate(run.turns)
        ]
        for turn in turns:
            for metric, score in turn.scores.items():
                judged.setdefault(metric, []).append(score.score)
        deterministic_ok = all(check.passed for check in checks)
        if "injection" in run.case.tags:
            injection.append(deterministic_ok)
        low = sum(not score.passed for turn in turns for score in turn.scores.values())
        cases.append(
            ReportCase(
                id=run.case.id,
                description=run.case.description,
                tags=list(run.case.tags),
                passed=deterministic_ok,
                low_scores=low,
                checks=[
                    ReportCheck(kind=c.kind, name=c.name, passed=c.passed, detail=c.detail)
                    for c in checks
                ],
                turns=turns,
                ended=run.ended,
                activated=run.activated,
                error=run.error,
            )
        )

    values: dict[str, float | None] = {
        metric: _share([c.passed for c in all_checks if c.kind == kind])
        for kind, metric in CHECK_METRICS.items()
    }
    values["injection_rejection"] = _share(injection)
    values["completed_runs"] = _share([run.error is None for run in runs])
    values |= {metric: _mean(found) for metric, found in judged.items()}

    gates = [
        Gate(
            metric=metric,
            value=None if values.get(metric) is None else round(values[metric] or 0.0, 4),
            threshold=threshold,
            # No data (e.g. no injection case in a subset) is not a failure.
            passed=values.get(metric) is None or (values[metric] or 0.0) >= threshold,
        )
        for metric, threshold in [*thresholds.deterministic.items(), *thresholds.judge.items()]
    ]
    by_tag: dict[str, TagSummary] = {}
    for case in cases:
        for tag in case.tags:
            entry = by_tag.setdefault(tag, TagSummary(cases=0, passed=0))
            entry.cases += 1
            entry.passed += case.passed

    summary = Summary(
        passed=all(gate.passed for gate in gates),
        cases=len(cases),
        cases_passed=sum(case.passed for case in cases),
        gates=gates,
        by_tag=dict(sorted(by_tag.items())),
        input_tokens=sum(run.usage.input_tokens for run in runs),
        output_tokens=sum(run.usage.output_tokens for run in runs),
        judge_cost_usd=None if judge_cost_usd is None else round(judge_cost_usd, 4),
        judge_errors=judge_errors,
    )
    return EvalReport(
        started_at=started_at,
        finished_at=finished_at,
        flow=flow,
        provider=provider,
        model=model,
        judge_model=judge_model,
        subset=subset,
        summary=summary,
        cases=cases,
    )


def _report_turn(record: TurnRecord, scores: TurnScores, thresholds: Thresholds) -> ReportTurn:
    return ReportTurn(
        user=record.user,
        reply=record.reply,
        skipped=record.skipped,
        tools=[
            ReportToolCall(name=call.name, args=dict(call.args), result=call.result)
            for call in record.tools
        ],
        sources=[f"[{s.marker}] {s.document_title} · {s.section or ''}" for s in record.sources],
        errors=record.errors,
        scores={
            metric: ReportScore(
                score=round(score.score, 4),
                reason=score.reason,
                passed=score.score >= thresholds.judge.get(metric, 0.0),
            )
            for metric, score in (scores or {}).items()
        },
        ttft_ms=record.timings.ttft_ms if record.timings else None,
        total_ms=record.timings.total_ms if record.timings else None,
    )


def _share(flags: Sequence[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


# --- Markdown --------------------------------------------------------------------------


def render_markdown(report: EvalReport) -> str:
    s = report.summary
    verdict = "PASS" if s.passed else "FAIL"
    cost = f", judge ≈ ${s.judge_cost_usd:.3f}" if s.judge_cost_usd is not None else ""
    lines = [
        f"# Eval report: {verdict}",
        "",
        f"- Flow `{report.flow}`, model `{report.provider}/{report.model}`, "
        f"judge `{report.judge_model or 'off'}`, subset `{report.subset}`",
        f"- {s.cases_passed}/{s.cases} cases passed; bot tokens {s.input_tokens} in / "
        f"{s.output_tokens} out{cost}",
        f"- {report.started_at:%Y-%m-%d %H:%M} UTC, "
        f"{(report.finished_at - report.started_at).total_seconds():.0f} s",
    ]
    if s.judge_errors:
        lines.append(f"- Judge errors: {s.judge_errors} turn(s) not scored")
    lines += ["", "| Metric | Value | Threshold | |", "| --- | --- | --- | --- |"]
    for gate in s.gates:
        value = "n/a" if gate.value is None else f"{gate.value:.2f}"
        lines.append(
            f"| {gate.metric} | {value} | {gate.threshold:.2f} | {'✅' if gate.passed else '❌'} |"
        )
    lines += ["", "| Tag | Passed |", "| --- | --- |"]
    lines += [f"| {tag} | {t.passed}/{t.cases} |" for tag, t in s.by_tag.items()]

    failed = [case for case in report.cases if not case.passed]
    lines += ["", f"## Failed cases ({len(failed)})"]
    if not failed:
        lines += ["", "None."]
    for case in failed:
        lines += ["", f"### `{case.id}` ({', '.join(case.tags)})", "", case.description, ""]
        if case.error:
            lines.append(f"- run error: {case.error}")
        failed_checks = [c for c in case.checks if not c.passed]
        lines += [f"- ❌ {c.name}{f': {c.detail}' if c.detail else ''}" for c in failed_checks]
        for number, turn in enumerate(case.turns, 1):
            if any(c.name.startswith(f"turn {number}:") for c in failed_checks):
                lines += [f"  - user: {turn.user}", f"  - bot: {_short(turn.reply)}"]

    scored = [case for case in report.cases if case.low_scores]
    lines += ["", f"## Low judge scores ({len(scored)} cases)"]
    if not scored:
        lines += ["", "None."]
    for case in scored:
        lines += ["", f"### `{case.id}`", ""]
        for number, turn in enumerate(case.turns, 1):
            low = {m: sc for m, sc in turn.scores.items() if not sc.passed}
            for metric, score in low.items():
                lines.append(f"- turn {number} {metric} {score.score:.2f}: {score.reason or ''}")
            if low:
                lines += [f"  - user: {turn.user}", f"  - bot: {_short(turn.reply)}"]
    return "\n".join(lines) + "\n"


def _short(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
