"""Index call_events by type and time for the ops summary (ROZ-70).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_call_events_type_ts", "call_events", ["type", "ts"])


def downgrade() -> None:
    op.drop_index("ix_call_events_type_ts", table_name="call_events")
