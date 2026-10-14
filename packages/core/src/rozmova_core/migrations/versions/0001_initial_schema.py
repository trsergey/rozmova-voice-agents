"""Initial schema: the SDD data model, pgvector, HNSW, GIN and jsonb_path_ops indexes.

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "access_codes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_access_codes_code_hash")),
    )
    op.create_table(
        "bots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("live_version_id", sa.UUID(), nullable=True),
        sa.Column("draft_flow", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("draft_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_eval_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bots")),
    )
    op.create_index(
        "ix_bots_draft_flow",
        "bots",
        ["draft_flow"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"draft_flow": "jsonb_path_ops"},
    )
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_documents")),
        sa.UniqueConstraint("source_path", name=op.f("uq_kb_documents_source_path")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('admin', 'viewer')", name=op.f("ck_users_role_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "bot_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("bot_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("flow", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("eval_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("version > 0", name=op.f("ck_bot_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["bot_id"], ["bots.id"], name=op.f("fk_bot_versions_bot_id_bots"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_bot_versions_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_versions")),
        sa.UniqueConstraint("bot_id", "version", name="uq_bot_versions_bot_id_version"),
    )
    # bots <-> bot_versions is a cycle: the live-version pointer is added once both exist.
    op.create_foreign_key(
        op.f("fk_bots_live_version_id_bot_versions"),
        "bots",
        "bot_versions",
        ["live_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_bot_versions_flow",
        "bot_versions",
        ["flow"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"flow": "jsonb_path_ops"},
    )
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, section || ' ' || text)", persisted=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["kb_documents.id"],
            name=op.f("fk_kb_chunks_document_id_kb_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
        sa.UniqueConstraint("document_id", "position", name="uq_kb_chunks_document_id_position"),
    )
    op.create_index(
        "ix_kb_chunks_embedding",
        "kb_chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], unique=False, postgresql_using="gin")
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("bot_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bots.id"],
            name=op.f("fk_provider_credentials_bot_id_bots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_credentials")),
        sa.UniqueConstraint("bot_id", "provider", name="uq_provider_credentials_bot_id_provider"),
    )
    op.create_table(
        "calls",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("bot_id", sa.UUID(), nullable=False),
        sa.Column("bot_version_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("access_code_id", sa.UUID(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["access_code_id"],
            ["access_codes.id"],
            name=op.f("fk_calls_access_code_id_access_codes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"], ["bots.id"], name=op.f("fk_calls_bot_id_bots"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["bot_version_id"],
            ["bot_versions.id"],
            name=op.f("fk_calls_bot_version_id_bot_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_calls_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calls")),
    )
    op.create_index("ix_calls_bot_id_started_at", "calls", ["bot_id", "started_at"], unique=False)
    op.create_table(
        "call_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("call_id", sa.UUID(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], name=op.f("fk_call_events_call_id_calls"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_events")),
    )
    op.create_index("ix_call_events_call_id_ts", "call_events", ["call_id", "ts"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_call_events_call_id_ts", table_name="call_events")
    op.drop_table("call_events")
    op.drop_index("ix_calls_bot_id_started_at", table_name="calls")
    op.drop_table("calls")
    op.drop_table("provider_credentials")
    op.drop_index("ix_kb_chunks_tsv", table_name="kb_chunks", postgresql_using="gin")
    op.drop_index(
        "ix_kb_chunks_embedding",
        table_name="kb_chunks",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("kb_chunks")
    op.drop_constraint(op.f("fk_bots_live_version_id_bot_versions"), "bots", type_="foreignkey")
    op.drop_index(
        "ix_bot_versions_flow",
        table_name="bot_versions",
        postgresql_using="gin",
        postgresql_ops={"flow": "jsonb_path_ops"},
    )
    op.drop_table("bot_versions")
    op.drop_table("users")
    op.drop_table("kb_documents")
    op.drop_index(
        "ix_bots_draft_flow",
        table_name="bots",
        postgresql_using="gin",
        postgresql_ops={"draft_flow": "jsonb_path_ops"},
    )
    op.drop_table("bots")
    op.drop_table("access_codes")
    op.execute("DROP EXTENSION IF EXISTS vector")
