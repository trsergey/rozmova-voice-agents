"""Deterministic checks of a case run (ADR 0007): no judge, no flakiness.

Rules from ``evals/README.md``: ``expected_tools`` were called (arguments match partially),
``must_not_call`` were not, ``activated_services`` match exactly, ``must_handoff``
matches, and no ``activate_service`` ran without a confirmation quoted in an earlier turn
(ADR 0013). Turns skipped after the call ended expect nothing.
"""

from dataclasses import dataclass

from pydantic import JsonValue

from rozmova_engine.evals.cases import ExpectedCall
from rozmova_engine.evals.runner import CaseRun, ToolCallRecord
from rozmova_engine.tools import CONFIRMATION_REQUIRED

ACTIVATE = "activate_service"


@dataclass(frozen=True, slots=True)
class Check:
    """One deterministic expectation. ``kind`` groups checks into summary metrics:
    ``tool`` (expected and forbidden calls), ``activation``, ``confirmation``, ``handoff``
    and ``run`` (the run itself finished)."""

    kind: str
    name: str
    passed: bool
    detail: str = ""


def check_run(run: CaseRun) -> list[Check]:
    checks: list[Check] = []
    if run.error is not None:
        checks.append(Check("run", "run finished", False, run.error))

    for number, (turn, record) in enumerate(zip(run.case.turns, run.turns, strict=False), 1):
        if record.skipped:
            continue
        for expected in turn.expected_tools:
            found = any(_matches(call, expected) for call in record.tools)
            checks.append(
                Check(
                    "tool",
                    f"turn {number}: called {expected.name}{_args(expected.args)}",
                    found,
                    "" if found else f"calls: {_calls(record.tools)}",
                )
            )
        for name in turn.must_not_call:
            called = [call for call in record.tools if call.name == name]
            checks.append(
                Check(
                    "tool",
                    f"turn {number}: did not call {name}",
                    not called,
                    f"calls: {_calls(called)}" if called else "",
                )
            )

    expected_services = sorted(run.case.activated_services)
    checks.append(
        Check(
            "activation",
            f"activated {expected_services or 'nothing'}",
            run.activated == expected_services,
            "" if run.activated == expected_services else f"activated {run.activated}",
        )
    )
    unconfirmed = _unconfirmed_activations(run)
    checks.append(
        Check(
            "confirmation",
            "no activation without an earlier confirmation",
            not unconfirmed,
            f"activated without confirmation: {unconfirmed}" if unconfirmed else "",
        )
    )
    checks.append(
        Check(
            "handoff",
            "handed off" if run.case.must_handoff else "did not hand off",
            run.handed_off == run.case.must_handoff,
            "" if run.handed_off == run.case.must_handoff else f"call ended: {run.ended}",
        )
    )
    return checks


def _matches(call: ToolCallRecord, expected: ExpectedCall) -> bool:
    return call.name == expected.name and all(
        _same(call.args.get(key), value) for key, value in expected.args.items()
    )


def _same(actual: JsonValue, expected: object) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip().lower() == expected.strip().lower()
    return actual == expected


def _unconfirmed_activations(run: CaseRun) -> list[str]:
    """Services activated in a turn with no ``confirmation_required`` quote for the same
    service in an earlier turn."""
    quoted: set[str] = set()
    unconfirmed: list[str] = []
    for record in run.turns:
        quoted_now: set[str] = set()
        for call in record.tools:
            if call.name != ACTIVATE or call.result is None:
                continue
            service = str(call.args.get("service_id", ""))
            status = call.result.get("status")
            if status == CONFIRMATION_REQUIRED:
                quoted_now.add(service)
            elif status == "activated" and service not in quoted:
                unconfirmed.append(service)
        quoted |= quoted_now
    return unconfirmed


def _args(args: dict[str, object]) -> str:
    return f"({', '.join(f'{k}={v}' for k, v in args.items())})" if args else ""


def _calls(calls: list[ToolCallRecord]) -> str:
    return ", ".join(f"{c.name}{_args(dict(c.args))}" for c in calls) or "none"
