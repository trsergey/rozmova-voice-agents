"""The background KB ingest admin-api starts with its lifespan."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from admin_api import kb
from rozmova_core.settings import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "kb_ingest_on_start": True,
        "openai_api_key": SecretStr("sk-test"),
        "kb_path": Path("kb"),
    }
    return Settings(**(values | overrides))


ENGINE: AsyncEngine = None  # type: ignore[assignment]  # never touched: ingest is replaced


def test_disabled_or_keyless_ingest_does_not_start() -> None:
    assert kb.start_ingest(_settings(kb_ingest_on_start=False), ENGINE) is None
    assert kb.start_ingest(_settings(openai_api_key=None), ENGINE) is None


async def test_stop_cancels_a_running_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def hang(*_: object) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(kb, "ingest_directory", hang)
    task = kb.start_ingest(_settings(), ENGINE)
    assert task is not None
    await started.wait()

    await kb.stop_ingest(task)

    assert task.cancelled()


async def test_failed_ingest_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_: object) -> None:
        raise ConnectionError("OpenAI is down")

    monkeypatch.setattr(kb, "ingest_directory", fail)
    task = kb.start_ingest(_settings(), ENGINE)
    assert task is not None

    await task

    await kb.stop_ingest(task)  # a finished task is left alone
    assert task.exception() is None


async def test_stop_without_a_task_is_a_no_op() -> None:
    await kb.stop_ingest(None)
