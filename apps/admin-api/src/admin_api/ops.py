"""The "Операції" page's numbers (FR-20): the demo runs without Grafana, so admin-api
computes a small health summary itself.

- ``active_calls``: open calls in Redis (``rozmova_core.open_calls``), fresh members only.
- Per window (last hour, last day), from ``call_events``: calls started, turns, p95 of the
  turn, time to first token, LLM first token, knowledge-base search and tool time (each
  ``tool.<name>`` stage of a turn is one sample); tool calls that returned an error and
  their share; ``error`` events per turn.

The result is cached for ``CACHE_SECONDS`` per process: the page polls, and the queries
scan a day of events.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import current_session
from rozmova_core import open_calls
from rozmova_core.settings import Settings

log = structlog.get_logger(__name__)

CACHE_SECONDS = 10.0
# Matches the gateway: an open-call entry older than the longest call plus this is stale.
STALE_MARGIN_SECONDS = 60
Window = Literal["1h", "24h"]
WINDOWS: dict[Window, timedelta] = {"1h": timedelta(hours=1), "24h": timedelta(hours=24)}


class P95(BaseModel):
    """Milliseconds; ``None`` when the window has no samples."""

    turn: float | None
    ttft: float | None
    llm_ttft: float | None
    rag_search: float | None
    tool: float | None


class WindowStats(BaseModel):
    window: Window
    calls: int
    turns: int
    p95_ms: P95
    tool_calls: int
    tool_errors: int
    tool_error_rate: float | None
    errors: int
    error_rate: float | None  # error events per turn


class OpsSummary(BaseModel):
    generated_at: datetime
    # None when Redis did not answer: unknown, not zero.
    active_calls: int | None
    windows: list[WindowStats]


_TURNS = text(
    """
    SELECT
      count(*) AS turns,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->'timings'->>'total_ms')::float),
      percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->'timings'->>'ttft_ms')::float),
      percentile_cont(0.95) WITHIN GROUP (
        ORDER BY (payload->'timings'->'stages'->>'llm_ttft')::float),
      percentile_cont(0.95) WITHIN GROUP (
        ORDER BY (payload->'timings'->'stages'->>'rag_search')::float)
    FROM call_events
    WHERE type = 'turn_end' AND ts >= :since
    """
)
_TOOL_TIME = text(
    """
    SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY stage.value::float)
    FROM call_events, jsonb_each_text(payload->'timings'->'stages') AS stage
    WHERE type = 'turn_end' AND ts >= :since AND stage.key LIKE 'tool.%'
    """
)
_COUNTS = text(
    """
    SELECT
      count(*) FILTER (WHERE type = 'session_started' AND NOT coalesce(
        (payload->>'resumed')::boolean, false)) AS calls,
      count(*) FILTER (WHERE type = 'tool_result') AS tool_calls,
      count(*) FILTER (WHERE type = 'tool_result'
        AND jsonb_typeof(payload->'result') = 'object' AND payload->'result' ? 'error')
        AS tool_errors,
      count(*) FILTER (WHERE type = 'error') AS errors
    FROM call_events
    WHERE type IN ('session_started', 'tool_result', 'error') AND ts >= :since
    """
)


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


async def _window(db: AsyncSession, name: Window, since: datetime) -> WindowStats:
    params = {"since": since}
    turns, p_turn, p_ttft, p_llm, p_rag = (await db.execute(_TURNS, params)).one()
    p_tool = await db.scalar(_TOOL_TIME, params)
    calls, tool_calls, tool_errors, errors = (await db.execute(_COUNTS, params)).one()
    return WindowStats(
        window=name,
        calls=calls,
        turns=turns,
        p95_ms=P95(turn=p_turn, ttft=p_ttft, llm_ttft=p_llm, rag_search=p_rag, tool=p_tool),
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        tool_error_rate=_rate(tool_errors, tool_calls),
        errors=errors,
        error_rate=_rate(errors, turns),
    )


async def _active_calls(redis: Redis, settings: Settings) -> int | None:
    fresh_since = time.time() - settings.limit_session_seconds - STALE_MARGIN_SECONDS
    try:
        total = 0
        async for key in redis.scan_iter(match=open_calls.PATTERN, count=500):
            total += await redis.zcount(key, fresh_since, "+inf")
    except (RedisError, OSError) as err:
        log.warning("ops_active_calls_unavailable", error=str(err))
        return None
    return total


async def build_summary(
    db: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> OpsSummary:
    now = datetime.now(UTC)
    async with db() as session:
        windows = [await _window(session, name, now - span) for name, span in WINDOWS.items()]
    return OpsSummary(
        generated_at=now, active_calls=await _active_calls(redis, settings), windows=windows
    )


class SummaryCache:
    """The last summary for ``CACHE_SECONDS``; concurrent misses share one computation."""

    def __init__(self) -> None:
        self._value: OpsSummary | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def get(
        self, db: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
    ) -> OpsSummary:
        async with self._lock:
            if self._value is None or time.monotonic() >= self._expires:
                self._value = await build_summary(db, redis, settings)
                self._expires = time.monotonic() + CACHE_SECONDS
            return self._value


router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(current_session)])


@router.get("/summary")
async def ops_summary(request: Request) -> OpsSummary:
    cache: SummaryCache = request.app.state.ops_cache
    return await cache.get(
        request.app.state.db, request.app.state.redis, request.app.state.settings
    )
