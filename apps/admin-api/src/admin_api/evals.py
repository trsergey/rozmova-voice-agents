"""Evals over a bot's draft and the publish gate (FR-19, SDD "Admin API").

``POST /bots/{id}/evals`` starts a background run of the eval set over the saved draft with
the same runner as ``make eval`` and CI (ADR 0011) and answers ``202 {job_id}`` at once: a
run takes minutes and costs money. One run per bot: while one is running, the same request
returns its ``job_id`` (``eval-job:bot:{id}`` in Redis, so it holds across processes). The
job's status and progress live in the Redis hash ``eval-job:{id}`` for an hour; the SPA
polls ``GET /evals/{job_id}``.

The run is the fast subset without the LLM judge (the first cases of each tag, as on PRs):
the deterministic checks are the hard guarantees (ADR 0007), and a judged full run is too
slow and costly for a button. The LLM key is the bot's own or the demo key, as on a live
call (``rozmova_engine.providers.keys``). The report goes to ``bots.draft_eval_report``
with the hash of the draft it ran on, so editing the draft makes it stale while moving
nodes on the canvas does not.

Publish (``require_green_eval``) needs that report for the current draft hash and passed;
``ROZMOVA_EVAL_GATE_BYPASS`` turns the gate off. Rollback and the demo seed do not pass the
gate: they put a flow live that has been live before or ships with the release.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly, current_session
from admin_api.bots import KNOWN_TOOLS, StoredEvalReport, draft_flow, get_bot
from admin_api.errors import ApiError
from rozmova_contracts.flow import FlowGraph
from rozmova_core.credentials import CredentialCipher
from rozmova_core.models import Bot, ProviderCredential
from rozmova_core.settings import Settings
from rozmova_engine.evals.cases import EvalCase, fast_subset, load_cases
from rozmova_engine.evals.report import EvalReport, Thresholds, build_report, load_thresholds
from rozmova_engine.evals.runner import Bot as EvalBot
from rozmova_engine.evals.runner import CaseRun, run_cases
from rozmova_engine.flow import validate_flow
from rozmova_engine.providers.keys import DemoKeySettings, Providers
from rozmova_engine.rag.answer import KnowledgeBase, KnowledgeSearch
from rozmova_engine.rag.embeddings import OpenAIEmbedder

log = structlog.get_logger(__name__)

JOB_TTL_S = 3600
# A whole run may take this long; a stuck provider must not hold the bot's slot forever.
RUN_TIMEOUT_S = 15 * 60


def _job_key(job_id: uuid.UUID) -> str:
    return f"eval-job:{job_id}"


def _bot_key(bot_id: uuid.UUID) -> str:
    return f"eval-job:bot:{bot_id}"


class EvalJobOut(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    state: Literal["running", "done", "failed"]
    draft_hash: str
    cases_total: int
    cases_done: int = 0
    passed: bool | None = None  # set when done
    # Set when failed: "cancelled", "timeout" or "run_failed"; details are in the logs only,
    # provider errors may quote the key.
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class EvalJobStarted(BaseModel):
    job_id: uuid.UUID


async def _save(redis: Redis, job: EvalJobOut) -> None:
    fields: dict[Any, Any] = {k: json.dumps(v) for k, v in job.model_dump(mode="json").items()}
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(_job_key(job.id), mapping=fields)
        pipe.expire(_job_key(job.id), JOB_TTL_S)
        await pipe.execute()


async def read_job(redis: Redis, job_id: uuid.UUID) -> EvalJobOut | None:
    fields = await redis.hgetall(_job_key(job_id))
    if not fields:
        return None
    return EvalJobOut.model_validate({_text(k): json.loads(v) for k, v in fields.items()})


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


@dataclass(frozen=True, slots=True)
class EvalRuntime:
    """What eval runs call: LLM clients per bot key, and the knowledge base (``None``: no
    key for query embeddings, so no runs)."""

    providers: Providers
    kb: KnowledgeSearch | None


def default_runtime(settings: Settings, db: async_sessionmaker[AsyncSession]) -> EvalRuntime:
    """The demo key from ``ROZMOVA_DEMO_*`` as on the gateway, else ``OPENAI_API_KEY`` (one
    OpenAI key serves the LLM and the embeddings on the demo); the bot's own key wins."""
    demo = DemoKeySettings()
    if demo.api_key is None and demo.provider == "openai":
        demo = demo.model_copy(update={"api_key": settings.openai_api_key})
    providers = Providers.from_settings(demo, CredentialCipher.from_settings(settings))
    key = settings.openai_api_key or (demo.api_key if demo.provider == "openai" else None)
    if key is None:
        return EvalRuntime(providers, None)
    return EvalRuntime(
        providers, KnowledgeBase(db, OpenAIEmbedder.create(key, settings.embedding_model))
    )


class EvalJobs:
    """Owns this process's eval runs; ``aclose`` cancels them on shutdown."""

    def __init__(
        self,
        evals_path: Path,
        redis: Redis,
        db: async_sessionmaker[AsyncSession],
        runtime: EvalRuntime,
    ) -> None:
        self._evals_path = evals_path
        self._redis = redis
        self._db = db
        self._providers = runtime.providers
        self._kb = runtime.kb
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, bot_id: uuid.UUID) -> uuid.UUID:
        """The running job of the bot, or a new one over its saved draft."""
        kb = self._kb
        if kb is None:
            raise ApiError(503, "eval_unavailable", "No OpenAI key for knowledge-base search.")
        async with self._db() as db:
            bot = await get_bot(db, bot_id)
            flow = draft_flow(bot)
            ciphertext = await db.scalar(
                select(ProviderCredential.secret_ciphertext).where(
                    ProviderCredential.bot_id == bot_id,
                    ProviderCredential.provider == bot.provider,
                )
            )
        result = validate_flow(flow, KNOWN_TOOLS)
        if not result.ok:
            raise ApiError(
                422,
                "draft_invalid",
                f"The draft has {len(result.errors)} error(s); fix them before an eval.",
                {"errors": [issue.model_dump(mode="json") for issue in result.errors]},
            )
        if not self._providers.has_key(bot.provider, ciphertext):
            raise ApiError(503, "eval_unavailable", f"No {bot.provider} key for this bot.")
        cases, thresholds = self._eval_set()

        job_id = uuid.uuid4()
        claimed = await self._redis.set(_bot_key(bot_id), str(job_id), nx=True, ex=JOB_TTL_S)
        if not claimed:
            running = await self._redis.get(_bot_key(bot_id))
            if running is not None:
                return uuid.UUID(_text(running))
            # The slot freed in between; a second click is rare enough to just ask again.
            raise ApiError(409, "eval_busy", "Try again.")
        job = EvalJobOut(
            id=job_id,
            bot_id=bot_id,
            state="running",
            draft_hash=flow.logic_hash(),
            cases_total=len(cases),
            started_at=datetime.now(UTC),
        )
        await _save(self._redis, job)
        eval_bot = EvalBot(flow, self._providers.get(bot.provider, ciphertext), bot.model, kb)
        task = asyncio.create_task(
            self._run(job, eval_bot, bot.provider, cases, thresholds), name=f"eval-{job_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("eval_started", bot_id=str(bot_id), job_id=str(job_id), cases=len(cases))
        return job_id

    def _eval_set(self) -> tuple[list[EvalCase], Thresholds]:
        try:
            cases = fast_subset(load_cases(self._evals_path / "cases"))
            thresholds = load_thresholds(self._evals_path / "thresholds.yaml")
        except (OSError, ValueError):
            log.exception("eval_set_unreadable", path=str(self._evals_path))
            raise ApiError(503, "eval_unavailable", "The eval set is not available.") from None
        return cases, thresholds

    async def _run(
        self,
        job: EvalJobOut,
        bot: EvalBot,
        provider: str,
        cases: list[EvalCase],
        thresholds: Thresholds,
    ) -> None:
        async def progress(_: CaseRun) -> None:
            await self._redis.hincrby(_job_key(job.id), "cases_done", 1)

        try:
            async with asyncio.timeout(RUN_TIMEOUT_S):
                runs = await run_cases(bot, cases, on_done=progress)
            report = build_report(
                runs,
                {},
                thresholds,
                started_at=job.started_at,
                finished_at=datetime.now(UTC),
                flow=f"draft {job.draft_hash[:12]}",
                provider=provider,
                model=bot.model,
                judge_model=None,
                judge_cost_usd=None,
                judge_errors=0,
                subset="fast",
            )
            await self._store(job, report)
            done = job.model_copy(
                update={
                    "state": "done",
                    "cases_done": len(runs),
                    "passed": report.summary.passed,
                    "finished_at": report.finished_at,
                }
            )
            log.info(
                "eval_finished",
                bot_id=str(job.bot_id),
                job_id=str(job.id),
                passed=report.summary.passed,
                cases_passed=report.summary.cases_passed,
                input_tokens=report.summary.input_tokens,
                output_tokens=report.summary.output_tokens,
            )
            await _save(self._redis, done)
        except asyncio.CancelledError:
            await self._fail(job, "cancelled")
            raise
        except TimeoutError:
            log.warning("eval_timeout", bot_id=str(job.bot_id), job_id=str(job.id))
            await self._fail(job, "timeout")
        except Exception:
            log.exception("eval_failed", bot_id=str(job.bot_id), job_id=str(job.id))
            await self._fail(job, "run_failed")
        finally:
            await self._release(job)

    async def _store(self, job: EvalJobOut, report: EvalReport) -> None:
        stored = StoredEvalReport(draft_hash=job.draft_hash, report=report)
        async with self._db() as db, db.begin():
            bot = await db.get(Bot, job.bot_id, with_for_update=True)
            if bot is not None:  # deleted while the run was going: nothing to keep
                bot.draft_eval_report = stored.model_dump(mode="json")

    async def _fail(self, job: EvalJobOut, error: str) -> None:
        done = await self._redis.hget(_job_key(job.id), "cases_done")
        failed = job.model_copy(
            update={
                "state": "failed",
                "error": error,
                "cases_done": int(done or 0),
                "finished_at": datetime.now(UTC),
            }
        )
        await _save(self._redis, failed)

    async def _release(self, job: EvalJobOut) -> None:
        # Only our own slot: after a TTL expiry another job may hold it.
        if await self._redis.get(_bot_key(job.bot_id)) == str(job.id):
            await self._redis.delete(_bot_key(job.bot_id))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            # wait() neither raises the tasks' CancelledError nor swallows our own.
            await asyncio.wait(self._tasks)
        await self._providers.aclose()
        close = getattr(self._kb, "aclose", None)
        if close is not None:
            await close()


def require_green_eval(bot: Bot, flow: FlowGraph) -> dict[str, Any]:
    """The eval report that lets ``flow`` (the bot's draft) go live; raises 409 otherwise.

    ``details.reason``: ``missing`` (no run yet), ``stale`` (the draft changed after the run)
    or ``failed`` (below the thresholds)."""
    if bot.draft_eval_report is None:
        raise ApiError(
            409, "eval_required", "Run an eval on the draft first.", {"reason": "missing"}
        )
    stored = StoredEvalReport.model_validate(bot.draft_eval_report)
    if stored.draft_hash != flow.logic_hash():
        raise ApiError(
            409,
            "eval_required",
            "The draft changed after the last eval; run it again.",
            {"reason": "stale"},
        )
    if not stored.report.summary.passed:
        raise ApiError(
            409,
            "eval_required",
            "The last eval of the draft is below the thresholds.",
            {"reason": "failed"},
        )
    return stored.report.model_dump(mode="json")


def _jobs(request: Request) -> EvalJobs:
    jobs: EvalJobs = request.app.state.evals
    return jobs


router = APIRouter(tags=["evals"], dependencies=[Depends(current_session)])


@router.post("/bots/{bot_id}/evals", status_code=202)
async def start_eval(bot_id: uuid.UUID, request: Request, _: AdminOnly) -> EvalJobStarted:
    return EvalJobStarted(job_id=await _jobs(request).start(bot_id))


@router.get("/evals/{job_id}")
async def get_eval(job_id: uuid.UUID, request: Request) -> EvalJobOut:
    job = await read_job(request.app.state.redis, job_id)
    if job is None:
        raise ApiError(404, "eval_job_not_found", "No such eval job, or it expired.")
    return job
