"""Knowledge base upkeep in admin-api: the background ingest on start (ROZ-27).

The reindex endpoint and the document list come with ROZ-35.
"""

import asyncio
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from rozmova_core.settings import Settings
from rozmova_engine.rag.embeddings import OpenAIEmbedder
from rozmova_engine.rag.ingest import ingest_directory

log = structlog.get_logger(__name__)


def start_ingest(settings: Settings, engine: AsyncEngine) -> asyncio.Task[None] | None:
    """Run the ingest in the background so a slow embedding API never delays the health
    check; the caller cancels the task on shutdown."""
    if not settings.kb_ingest_on_start:
        return None
    if settings.openai_api_key is None:
        log.warning("kb_ingest_skipped", reason="OPENAI_API_KEY unset")
        return None
    embedder = OpenAIEmbedder.create(settings.openai_api_key, settings.embedding_model)
    return asyncio.create_task(_ingest(settings.kb_path, engine, embedder), name="kb-ingest")


async def stop_ingest(task: asyncio.Task[None] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        # wait() neither raises the task's CancelledError nor swallows our own.
        await asyncio.wait([task])


async def _ingest(kb_path: Path, engine: AsyncEngine, embedder: OpenAIEmbedder) -> None:
    try:
        await ingest_directory(kb_path, engine, embedder)
        log.info("kb_embedding_usage", model=embedder.model, tokens=embedder.usage_tokens)
    except Exception:
        # The admin API stays useful without a fresh index; the error is in the logs.
        log.exception("kb_ingest_failed")
    finally:
        await embedder.aclose()
