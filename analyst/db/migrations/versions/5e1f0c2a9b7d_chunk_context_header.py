"""chunk context header: chunks.context, tsv over context + text

Revision ID: 5e1f0c2a9b7d
Revises: 183713853069
Create Date: 2026-11-23

The generated `tsv` column's expression changes, which Postgres can't ALTER, so the column
(and its GIN index) is dropped and re-added. Existing chunks get an empty context; the
indexer (PARSER_VERSION 2) rebuilds every filing's chunks with headers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5e1f0c2a9b7d"
down_revision: str | None = "183713853069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tsv(expr: str) -> None:
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_column("chunks", "tsv")
    op.add_column(
        "chunks",
        sa.Column("tsv", postgresql.TSVECTOR(), sa.Computed(expr, persisted=True), nullable=False),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")


def upgrade() -> None:
    op.add_column("chunks", sa.Column("context", sa.Text(), server_default="", nullable=False))
    _tsv("to_tsvector('english', context || ' ' || text)")


def downgrade() -> None:
    _tsv("to_tsvector('english', text)")
    op.drop_column("chunks", "context")
