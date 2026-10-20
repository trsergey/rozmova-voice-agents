"""The ops summary against a migrated PostgreSQL and a fake Redis (FR-20)."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete

from admin_api import ops
from admin_api.app import create_app
from admin_api.sessions import CSRF_HEADER
from rozmova_core import open_calls
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, Call, CallEvent, User
from rozmova_core.settings import Settings

pytestmark = pytest.mark.integration

ADMIN = {"email": "admin@example.com", "password": "admin-password"}


@pytest.fixture
def settings(database_url: SecretStr) -> Settings:
    return Settings(
        env="test",
        database_url=database_url,
        demo_seed=False,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
    )


def turn_end(total: float, ttft: float, stages: dict[str, float]) -> dict[str, Any]:
    return {"turn_id": 1, "timings": {"total_ms": total, "ttft_ms": ttft, "stages": stages}}


async def seed(settings: Settings, now: datetime) -> None:
    """Events of the last hour, of earlier today and of two days ago."""
    hour, today, old = (
        now - timedelta(minutes=10),
        now - timedelta(hours=3),
        now - timedelta(days=2),
    )
    events: list[tuple[datetime, str, dict[str, Any]]] = [
        (hour, "session_started", {"resumed": False}),
        (hour, "session_started", {"resumed": True}),  # a reconnect, not a new call
        (hour, "turn_end", turn_end(2, 0.1, {})),  # a greeting: no LLM, not in the p95
        (hour, "turn_end", turn_end(100, 50, {"llm_ttft": 40, "tool.get_balance": 10})),
        (hour, "turn_end", turn_end(200, 150, {"llm_ttft": 140, "rag_search": 30})),
        (hour, "tool_result", {"name": "get_balance", "result": {"balance": 1}}),
        (hour, "tool_result", {"name": "activate_service", "result": {"error": "timeout"}}),
        (hour, "error", {"code": "llm_unavailable", "message": "x"}),
        (today, "session_started", {"resumed": False}),
        (today, "turn_end", turn_end(1000, 900, {"llm_ttft": 800})),
        (old, "turn_end", turn_end(9000, 9000, {})),
    ]
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Bot))
        await db.execute(delete(User))
        bot = Bot(name="b", provider="openai", model="m")
        db.add(bot)
        await db.flush()
        call = Call(bot_id=bot.id, started_at=old)
        db.add(call)
        await db.flush()
        for n, (ts, kind, payload) in enumerate(events):
            db.add(CallEvent(id=f"{n}-0", call_id=call.id, ts=ts, type=kind, payload=payload))
    await engine.dispose()


async def open_call(redis: FakeAsyncRedis, user_id: str, started: float) -> None:
    await redis.zadd(open_calls.key(user_id), {str(uuid4()): started})


def test_summary(settings: Settings) -> None:
    now = datetime.now(UTC)
    asyncio.run(seed(settings, now))
    redis = FakeAsyncRedis(decode_responses=True)
    stale = time.time() - settings.limit_session_seconds - 120
    for user, started in (("user:a", time.time()), ("code:b", time.time()), ("code:b", stale)):
        asyncio.run(open_call(redis, user, started))

    with TestClient(create_app(settings, create_engine(settings), redis)) as http:
        session = http.post("/auth/login", json=ADMIN).json()
        http.headers[CSRF_HEADER] = session["csrf_token"]
        summary = http.get("/ops/summary").json()

    assert summary["active_calls"] == 2
    hour, day = summary["windows"]
    assert hour["window"] == "1h"
    assert (hour["calls"], hour["turns"], hour["tool_calls"], hour["tool_errors"]) == (1, 3, 2, 1)
    assert hour["tool_error_rate"] == 0.5
    assert (hour["errors"], hour["error_rate"]) == (1, pytest.approx(1 / 3))
    # percentile_cont interpolates: p95 of 100 and 200 is 195.
    assert hour["p95_ms"] == {
        "turn": 195.0,
        "ttft": 145.0,
        "llm_ttft": 135.0,
        "rag_search": 30.0,
        "tool": 10.0,
    }
    assert (day["window"], day["calls"], day["turns"]) == ("24h", 2, 4)
    assert day["p95_ms"]["turn"] == pytest.approx(920.0)  # 200 + 0.9 * (1000 - 200)


def test_empty_windows_have_no_numbers(settings: Settings) -> None:
    async def clear() -> None:
        engine = create_engine(settings)
        async with create_session_factory(engine)() as db, db.begin():
            await db.execute(delete(Bot))
        await engine.dispose()

    asyncio.run(clear())

    summary = asyncio.run(
        ops.build_summary(
            create_session_factory(create_engine(settings)), FakeAsyncRedis(), settings
        )
    )

    hour = summary.windows[0]
    assert (hour.turns, hour.calls, hour.tool_error_rate, hour.error_rate) == (0, 0, None, None)
    assert hour.p95_ms.turn is None
    assert summary.active_calls == 0


def test_summary_is_cached(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    built = 0

    async def build(*_: object) -> ops.OpsSummary:
        nonlocal built
        built += 1
        return ops.OpsSummary(generated_at=datetime.now(UTC), active_calls=0, windows=[])

    monkeypatch.setattr(ops, "build_summary", build)
    cache = ops.SummaryCache()

    async def twice() -> None:
        await asyncio.gather(*(cache.get(None, None, settings) for _ in range(3)))  # type: ignore[arg-type]

    asyncio.run(twice())
    assert built == 1


def test_active_calls_unknown_when_redis_fails(settings: Settings) -> None:
    class DownRedis(FakeAsyncRedis):
        def scan_iter(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    assert asyncio.run(ops._active_calls(DownRedis(), settings)) is None


def test_ops_needs_a_session(settings: Settings) -> None:
    http = TestClient(create_app(settings, create_engine(settings), FakeAsyncRedis()))

    assert http.get("/ops/summary").status_code == 401
