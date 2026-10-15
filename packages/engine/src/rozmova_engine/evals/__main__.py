"""``python -m rozmova_engine.evals``: run the eval set and write ``eval_report`` files.

``make eval`` runs every case, ``make eval-fast`` the first two of each tag. The bot needs
the local stack with an ingested knowledge base (``make up``, ``make ingest``) and an LLM
key; the judge needs the ``evals`` extra (``uv sync --all-packages --extra evals``).
Exit code 1 when a threshold is not met.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import SecretStr
from sqlalchemy import func, select

from rozmova_contracts.flow import FlowGraph
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.log import configure_logging
from rozmova_core.models import KbChunk
from rozmova_core.settings import Settings
from rozmova_engine.evals.cases import fast_subset, load_cases
from rozmova_engine.evals.judge import DeepEvalJudge, Judge
from rozmova_engine.evals.report import (
    build_report,
    judge_runs,
    load_thresholds,
    render_markdown,
)
from rozmova_engine.evals.runner import DEFAULT_CONCURRENCY, Bot, CaseRun, run_cases
from rozmova_engine.providers import ProviderConfig
from rozmova_engine.providers import registry as providers
from rozmova_engine.rag.answer import KnowledgeBase
from rozmova_engine.rag.embeddings import OpenAIEmbedder

log = structlog.get_logger(__name__)

ROOT = Path.cwd()
DEFAULT_MODEL = "gpt-4.1-mini"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m rozmova_engine.evals")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/cases")
    parser.add_argument("--flow", type=Path, default=ROOT / "flows/hvylia-demo.json")
    parser.add_argument("--thresholds", type=Path, default=ROOT / "evals/thresholds.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "evals/reports")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_MODEL)
    parser.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    parser.add_argument("--fast", action="store_true", help="the first 2 cases of each tag")
    parser.add_argument("--case", action="append", default=[], help="run only these ids")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    return parser.parse_args(argv)


def openai_key(settings: Settings) -> SecretStr | None:
    """``OPENAI_API_KEY``, or the demo key the gateway uses."""
    demo = os.environ.get("ROZMOVA_DEMO_API_KEY")
    return settings.openai_api_key or (SecretStr(demo) if demo else None)


async def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(settings)

    cases = load_cases(args.cases)
    subset = "full"
    if args.case:
        cases = [case for case in cases if case.id in set(args.case)]
        subset = "selected"
    elif args.fast:
        cases = fast_subset(cases)
        subset = "fast"
    thresholds = load_thresholds(args.thresholds)

    key = openai_key(settings)
    if key is None:
        print("OPENAI_API_KEY is not set (needed for query embeddings)", file=sys.stderr)
        return 2
    provider_key = key if args.provider == "openai" else None
    provider = providers.create(args.provider, ProviderConfig(api_key=provider_key))
    judge: Judge | None = None if args.no_judge else DeepEvalJudge(args.judge_model, api_key=key)

    engine = create_engine(settings)
    embedder = OpenAIEmbedder.create(key, settings.embedding_model)
    kb = KnowledgeBase(create_session_factory(engine), embedder)
    try:
        async with engine.connect() as conn:
            if not await conn.scalar(select(func.count()).select_from(KbChunk)):
                print("The knowledge base is empty: run `make ingest` first", file=sys.stderr)
                return 2
        bot = Bot(FlowGraph.model_validate_json(args.flow.read_text()), provider, args.model, kb)

        started = datetime.now(UTC)
        print(f"Running {len(cases)} case(s) on {args.provider}/{args.model}…", file=sys.stderr)
        runs = await run_cases(bot, cases, concurrency=args.concurrency, on_done=_progress)
        errors: list[str] = []
        scores = (
            await judge_runs(runs, judge, on_error=lambda c, t, e: errors.append(f"{c}#{t}"))
            if judge is not None
            else {}
        )
        report = build_report(
            runs,
            scores,
            thresholds,
            started_at=started,
            finished_at=datetime.now(UTC),
            flow=_shown(args.flow),
            provider=args.provider,
            model=args.model,
            judge_model=judge.model if judge else None,
            judge_cost_usd=judge.cost_usd if judge else None,
            judge_errors=len(errors),
            subset=subset,
        )
    finally:
        await kb.aclose()
        await engine.dispose()
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"eval_report-{started:%Y%m%d-%H%M%S}"
    json_path = args.out / f"{stem}.json"
    md_path = args.out / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"Report: {json_path}\n        {md_path}", file=sys.stderr)
    return 0 if report.summary.passed else 1


def _shown(path: Path) -> str:
    """The path relative to the working directory, so reports read the same everywhere."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _progress(run: CaseRun) -> None:
    from rozmova_engine.evals.checks import check_run

    ok = all(check.passed for check in check_run(run))
    print(f"  {'✓' if ok else '✗'} {run.case.id}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
