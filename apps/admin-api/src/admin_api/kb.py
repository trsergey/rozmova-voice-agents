"""Knowledge base in admin-api (FR-15): the background ingest, the reindex endpoint and the
document list.

The ingest runs in the background, both on start (ROZ-27, ``kb_ingest_on_start``) and on
``POST /kb/reindex``, so a slow embedding API never holds a request or the health check.
One ingest runs at a time in a process; the ingest's advisory lock covers other processes.
``GET /kb/reindex`` tells the SPA how the last run went.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly, current_session
from admin_api.errors import ApiError
from rozmova_core.models import KbChunk, KbDocument
from rozmova_core.settings import Settings
from rozmova_engine.rag.embeddings import OpenAIEmbedder
from rozmova_engine.rag.ingest import IngestReport, ingest_directory

log = structlog.get_logger(__name__)


class ReindexStatus(BaseModel):
    """``failed`` carries no error text: provider errors may quote the key; see the logs."""

    state: Literal["idle", "running", "done", "failed"] = "idle"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Set when done; ``skipped_locked`` means another process was already ingesting.
    report: IngestReport | None = None


class KbIndexer:
    """Owns the background ingest task of this process; ``aclose`` cancels it on shutdown."""

    def __init__(self, settings: Settings, engine: AsyncEngine) -> None:
        self._settings = settings
        self._engine = engine
        self._task: asyncio.Task[None] | None = None
        self.status = ReindexStatus()

    @property
    def available(self) -> bool:
        """Ingest embeds the chunks, so it needs the OpenAI key."""
        return self._settings.openai_api_key is not None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start an ingest unless one is already running."""
        key = self._settings.openai_api_key
        if key is None:
            log.warning("kb_ingest_skipped", reason="OPENAI_API_KEY unset")
            return
        if self.running:
            return
        embedder = OpenAIEmbedder.create(key, self._settings.embedding_model)
        self.status = ReindexStatus(state="running", started_at=datetime.now(UTC))
        self._task = asyncio.create_task(self._run(embedder), name="kb-ingest")

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            # wait() neither raises the task's CancelledError nor swallows our own.
            await asyncio.wait([self._task])

    async def _run(self, embedder: OpenAIEmbedder) -> None:
        started_at = self.status.started_at
        try:
            report = await ingest_directory(self._settings.kb_path, self._engine, embedder)
            log.info("kb_embedding_usage", model=embedder.model, tokens=embedder.usage_tokens)
            self.status = ReindexStatus(
                state="done", started_at=started_at, finished_at=datetime.now(UTC), report=report
            )
        except Exception:
            # The admin API stays useful without a fresh index; the error is in the logs.
            log.exception("kb_ingest_failed")
            self.status = ReindexStatus(
                state="failed", started_at=started_at, finished_at=datetime.now(UTC)
            )
        finally:
            await embedder.aclose()


class KbDocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source_path: str
    chunks: int
    updated_at: datetime


def _indexer(request: Request) -> KbIndexer:
    indexer: KbIndexer = request.app.state.kb
    return indexer


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


router = APIRouter(prefix="/kb", tags=["kb"], dependencies=[Depends(current_session)])


@router.get("/documents")
async def list_documents(request: Request) -> list[KbDocumentOut]:
    async with _db(request)() as db:
        rows = await db.execute(
            select(KbDocument, func.count(KbChunk.id))
            .outerjoin(KbChunk, KbChunk.document_id == KbDocument.id)
            .group_by(KbDocument.id)
            .order_by(KbDocument.source_path)
        )
        return [
            KbDocumentOut(
                id=doc.id,
                title=doc.title,
                source_path=doc.source_path,
                chunks=chunks,
                updated_at=doc.updated_at,
            )
            for doc, chunks in rows
        ]


@router.get("/reindex")
async def reindex_status(request: Request) -> ReindexStatus:
    return _indexer(request).status


@router.post("/reindex", status_code=202)
async def reindex(request: Request, admin: AdminOnly) -> ReindexStatus:
    """Starts an ingest, or reports the one already running; poll ``GET /kb/reindex``."""
    indexer = _indexer(request)
    if not indexer.available:
        raise ApiError(503, "embeddings_unavailable", "No OpenAI key to embed the documents.")
    if not indexer.running:
        indexer.start()
        log.info("kb_reindex_requested", user_id=admin.user_id)
    return indexer.status
