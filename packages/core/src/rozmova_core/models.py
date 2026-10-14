"""SQLAlchemy ORM models for the SDD data model ("Модель даних PostgreSQL").

The tables are shared by the gateway, admin-api and the engine (RAG reads ``kb_chunks``),
so they live in core. Alembic migrations in ``rozmova_core.migrations`` create them;
``tests/test_migrations.py`` checks that the migrations and these models stay in sync.
"""

import uuid
from datetime import datetime
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# text-embedding-3-small (ADR 0004).
EMBEDDING_DIMENSIONS = 1536

USER_ROLES = ("admin", "viewer")

# Deterministic constraint names, so migrations can drop them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Bot(Base):
    """A bot with one in-place draft and a pointer to its live version (ADR 0012)."""

    __tablename__ = "bots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    live_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bot_versions.id", ondelete="SET NULL", use_alter=True)
    )
    draft_flow: Mapped[dict[str, Any] | None]
    draft_updated_at: Mapped[datetime | None]
    draft_eval_report: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index(
            "ix_bots_draft_flow",
            "draft_flow",
            postgresql_using="gin",
            postgresql_ops={"draft_flow": "jsonb_path_ops"},
        ),
    )


class BotVersion(Base):
    """An immutable published flow; created only by publish or rollback."""

    __tablename__ = "bot_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    bot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    flow: Mapped[dict[str, Any]]
    note: Mapped[str | None] = mapped_column(Text)
    eval_report: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = _created_at()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("bot_id", "version", name="uq_bot_versions_bot_id_version"),
        CheckConstraint("version > 0", name="version_positive"),
        Index(
            "ix_bot_versions_flow",
            "flow",
            postgresql_using="gin",
            postgresql_ops={"flow": "jsonb_path_ops"},
        ),
    )


class ProviderCredential(Base):
    """A bot's LLM provider key, Fernet-encrypted; ``last4`` is shown masked in the admin."""

    __tablename__ = "provider_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    bot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text)
    secret_ciphertext: Mapped[str] = mapped_column(Text)
    last4: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("bot_id", "provider", name="uq_provider_credentials_bot_id_provider"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint(f"role IN {USER_ROLES!r}", name="role_valid"),)


class AccessCode(Base):
    """A per-company demo access code (viewer role); revoked by setting ``revoked_at``."""

    __tablename__ = "access_codes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    label: Mapped[str] = mapped_column(Text)
    code_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]


class Call(Base):
    """One row per call. ``bot_version_id`` is null for test-console calls on the draft."""

    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    bot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    bot_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bot_versions.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    access_code_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_codes.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_calls_bot_id_started_at", "bot_id", "started_at"),)


class CallEvent(Base):
    """A call timeline event (FR-13); ``id`` is the Redis Stream entry id, so writes are
    idempotent under at-least-once delivery."""

    __tablename__ = "call_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"))
    ts: Mapped[datetime]
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))

    __table_args__ = (Index("ix_call_events_call_id_ts", "call_id", "ts"),)


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text, unique=True)
    content_hash: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbChunk(Base):
    """A knowledge-base fragment for hybrid search (ADR 0004).

    ``tsv`` is generated by PostgreSQL from section and text with the ``simple`` config;
    ``content_hash`` lets ingest skip re-embedding unchanged chunks.
    """

    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple'::regconfig, section || ' ' || text)", persisted=True),
    )

    __table_args__ = (
        UniqueConstraint("document_id", "position", name="uq_kb_chunks_document_id_position"),
        Index(
            "ix_kb_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_kb_chunks_tsv", "tsv", postgresql_using="gin"),
    )
